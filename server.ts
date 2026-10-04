import express, { Request, Response, NextFunction } from 'express';
import path from 'path';
import fs from 'fs';
import crypto from 'crypto';
import zlib from 'zlib';
import AdmZip from 'adm-zip';
import { XMLParser } from 'fast-xml-parser';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const ROOT = __dirname;

const PORT = 3000;
const HOST = '0.0.0.0';
const APP_PASSWORD = process.env.APP_PASSWORD || '';
const COOKIE_SECURE = process.env.COOKIE_SECURE === 'true' || process.env.COOKIE_SECURE === '1';
const CONFIG_DIR = path.resolve(process.env.CONFIG_DIR || path.join(ROOT, 'config'));
const CONFIG_FILE = path.join(CONFIG_DIR, 'calibre-web.json');
const SHELF_FILE = path.join(CONFIG_DIR, 'bookshelf.json');
const HISTORY_FILE = path.join(CONFIG_DIR, 'history.json');
const EPUB_CACHE_DIR = path.join(CONFIG_DIR, 'epub-cache');

try {
  fs.mkdirSync(CONFIG_DIR, { recursive: true });
  fs.mkdirSync(EPUB_CACHE_DIR, { recursive: true });
} catch {
  // directory might exist or be read-only in some environments
}

// In-memory fallbacks & caches
interface OpdsSessionItem {
  username?: string;
  password?: string;
  address?: string;
  download?: string;
  cover?: string;
  title?: string;
  author?: string;
  description?: string;
  shelfKey?: string;
  format?: string;
  feed?: string;
  expires: number;
}

const sessions = new Map<string, OpdsSessionItem>();
const authSessions = new Map<string, number>();
const loginFailures = new Map<string, number[]>();
const recommendationCache = new Map<string, { expires: number; books: any[] }>();

// Book shelf & history in-memory caches
let inMemoryShelf: any[] = [];
let inMemoryHistory: any[] = [];
let inMemoryConfig: any = null;

function loadBookshelf(): any[] {
  try {
    if (fs.existsSync(SHELF_FILE)) {
      const data = JSON.parse(fs.readFileSync(SHELF_FILE, 'utf-8'));
      if (Array.isArray(data)) {
        inMemoryShelf = data;
        return data;
      }
    }
  } catch (err) {
    console.warn('Failed to read shelf file, using in-memory store', err);
  }
  return inMemoryShelf;
}

function saveBookshelf(books: any[]) {
  inMemoryShelf = books;
  try {
    fs.mkdirSync(CONFIG_DIR, { recursive: true });
    fs.writeFileSync(SHELF_FILE, JSON.stringify(books, null, 2), 'utf-8');
  } catch (err) {
    console.warn('Failed to write shelf file, kept in-memory', err);
  }
}

function loadReadingHistory(): any[] {
  try {
    if (fs.existsSync(HISTORY_FILE)) {
      const data = JSON.parse(fs.readFileSync(HISTORY_FILE, 'utf-8'));
      if (Array.isArray(data)) {
        inMemoryHistory = data;
        return data;
      }
    }
  } catch (err) {
    console.warn('Failed to read history file, using in-memory store', err);
  }
  return inMemoryHistory;
}

function saveReadingHistory(records: any[]) {
  inMemoryHistory = records;
  try {
    fs.mkdirSync(CONFIG_DIR, { recursive: true });
    fs.writeFileSync(HISTORY_FILE, JSON.stringify(records, null, 2), 'utf-8');
  } catch (err) {
    console.warn('Failed to write history file, kept in-memory', err);
  }
}

function loadConnectionConfig(): any {
  try {
    if (fs.existsSync(CONFIG_FILE)) {
      const data = JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf-8'));
      inMemoryConfig = data;
      return data;
    }
  } catch (err) {
    console.warn('Failed to read connection config, using in-memory', err);
  }
  return inMemoryConfig;
}

function saveConnectionConfig(address: string, username: string, password: string) {
  inMemoryConfig = { address, username, password };
  try {
    fs.mkdirSync(CONFIG_DIR, { recursive: true });
    fs.writeFileSync(CONFIG_FILE, JSON.stringify(inMemoryConfig, null, 2), 'utf-8');
  } catch (err) {
    console.warn('Failed to write config file, kept in-memory', err);
  }
}

function deleteConnectionConfig() {
  inMemoryConfig = null;
  try {
    if (fs.existsSync(CONFIG_FILE)) {
      fs.unlinkSync(CONFIG_FILE);
    }
  } catch {
    // Ignore
  }
}

function originOf(urlStr: string): string {
  try {
    const parsed = new URL(urlStr);
    return parsed.origin.toLowerCase();
  } catch {
    throw new Error('书库链接必须使用有效的 HTTP 或 HTTPS 地址。');
  }
}

function libraryIdentity(urlStr: string): string {
  const parsed = new URL(urlStr);
  const path = parsed.pathname.replace(/\/+$/, '') || '/';
  return `${parsed.origin}${path}`;
}

function sameOriginUrl(base: string, href: string): string {
  const target = new URL(href, base).href;
  if (originOf(target) !== originOf(base)) {
    throw new Error('Calibre-Web 目录包含跨站链接，已阻止访问。');
  }
  return target;
}

function libraryIds(urlStr: string): Set<string> {
  const origin = originOf(urlStr);
  return new Set([origin, libraryIdentity(urlStr)]);
}

function shelfKey(download: string, title: string, author: string): string {
  const identity = `${download}\0${title}\0${author}`;
  return crypto.createHash('sha256').update(identity).digest('hex').slice(0, 32);
}

function getOpdsSession(token: string, id: string): OpdsSessionItem | null {
  const key = `${token}:${id}`;
  const item = sessions.get(key);
  if (!item) return null;
  if (item.expires <= Date.now()) {
    sessions.delete(key);
    return null;
  }
  return item;
}

function putOpdsSession(token: string, id: string, item: OpdsSessionItem) {
  const key = `${token}:${id}`;
  sessions.set(key, item);
  if (sessions.size > 8192) {
    const firstKey = sessions.keys().next().value;
    if (firstKey) sessions.delete(firstKey);
  }
}

async function fetchRemote(
  urlStr: string,
  username = '',
  password = '',
  accept = '*/*',
  extraHeaders: Record<string, string> = {},
  timeoutMs = 25000
): Promise<globalThis.Response> {
  const headers: Record<string, string> = {
    Accept: accept,
    'User-Agent': 'PagesBetween/1.0',
    ...extraHeaders
  };
  if (username || password) {
    const cred = Buffer.from(`${username}:${password}`).toString('base64');
    headers['Authorization'] = `Basic ${cred}`;
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(urlStr, {
      headers,
      signal: controller.signal
    });
    return res;
  } finally {
    clearTimeout(timer);
  }
}

// XML parser configured for Atom / OPDS
const xmlParser = new XMLParser({
  ignoreAttributes: false,
  attributeNamePrefix: '@_',
  textNodeName: '#text',
  trimValues: true,
  removeNSPrefix: true
});

function parseOpdsFeed(xmlText: string, address: string, username: string, password: string, token: string) {
  let parsed: any;
  try {
    parsed = xmlParser.parse(xmlText);
  } catch (err: any) {
    throw new Error('OPDS 目录格式无法识别: ' + (err.message || ''));
  }

  const feed = parsed.feed || parsed;
  let entryList = feed.entry;
  if (!entryList) {
    entryList = [];
  } else if (!Array.isArray(entryList)) {
    entryList = [entryList];
  }

  const entries: any[] = [];
  for (const node of entryList) {
    let links = node.link;
    if (!links) links = [];
    else if (!Array.isArray(links)) links = [links];

    function getLinkRel(l: any) {
      return (l['@_rel'] || '').toLowerCase();
    }
    function getLinkType(l: any) {
      return (l['@_type'] || '').toLowerCase();
    }
    function getLinkHref(l: any) {
      return l['@_href'] || '';
    }

    const acquisitions = links.filter((l: any) => {
      const rel = getLinkRel(l);
      const typ = getLinkType(l);
      return (
        rel.includes('acquisition') ||
        typ.includes('epub') ||
        typ.includes('ebook') ||
        typ.includes('pdf') ||
        typ.includes('text/plain')
      );
    });

    function formatOf(l: any) {
      const hint = `${getLinkType(l)} ${getLinkHref(l)}`.toLowerCase();
      if (hint.includes('epub')) return 'epub';
      if (hint.includes('pdf')) return 'pdf';
      if (hint.includes('text/plain') || hint.split('?')[0].endsWith('.txt')) return 'txt';
      return 'other';
    }

    let acquisition = acquisitions.find((l: any) => ['epub', 'pdf', 'txt'].includes(formatOf(l)));
    if (!acquisition && acquisitions.length > 0) {
      acquisition = acquisitions[0];
    }
    const bookFormat = acquisition ? formatOf(acquisition) : '';

    const navigation = links.find((l: any) => {
      const rel = getLinkRel(l);
      const typ = getLinkType(l);
      return rel.includes('subsection') || (typ.includes('opds-catalog') && !acquisition);
    });

    const cover = links.find((l: any) => {
      const rel = getLinkRel(l);
      const typ = getLinkType(l);
      return rel.includes('image') || typ.includes('image') || rel.includes('thumbnail');
    });

    const itemId = crypto.randomUUID().replace(/-/g, '');
    let title = typeof node.title === 'string' ? node.title : (node.title?.['#text'] || '未命名书籍');
    let author = '未知作者';
    if (node.author) {
      if (typeof node.author === 'string') author = node.author;
      else if (node.author.name) author = typeof node.author.name === 'string' ? node.author.name : (node.author.name?.['#text'] || '未知作者');
    }

    let description = '';
    const descNode = node.summary || node.content || node.description;
    if (descNode) {
      if (typeof descNode === 'string') description = descNode;
      else if (descNode['#text']) description = descNode['#text'];
      description = description.replace(/<[^>]+>/g, '').trim().slice(0, 5000);
    }

    let download = '';
    if (acquisition && getLinkHref(acquisition)) {
      try {
        download = sameOriginUrl(address, getLinkHref(acquisition));
      } catch {
        // ignore
      }
    }

    let coverUrl = '';
    if (cover && getLinkHref(cover)) {
      try {
        coverUrl = sameOriginUrl(address, getLinkHref(cover));
      } catch {
        // ignore
      }
    }

    let feedUrl = '';
    if (navigation && getLinkHref(navigation)) {
      try {
        feedUrl = sameOriginUrl(address, getLinkHref(navigation));
      } catch {
        // ignore
      }
    }

    const stableKey = download ? shelfKey(download, title, author) : '';
    const expires = Date.now() + 12 * 60 * 60 * 1000;

    putOpdsSession(token, itemId, {
      username,
      password,
      address,
      download,
      cover: coverUrl,
      title,
      author,
      description,
      shelfKey: stableKey,
      format: bookFormat,
      feed: feedUrl,
      expires
    });

    entries.push({
      id: itemId,
      title,
      author,
      description,
      tag: navigation ? '目录' : (bookFormat ? bookFormat.toUpperCase() : 'OPDS'),
      category: 'all',
      kind: navigation ? 'navigation' : 'book',
      format: bookFormat,
      shelfKey: stableKey,
      href: navigation
        ? `/api/opds?token=${encodeURIComponent(token)}&id=${encodeURIComponent(itemId)}`
        : (download && ['epub', 'pdf', 'txt'].includes(bookFormat)
          ? `/api/read?token=${encodeURIComponent(token)}&id=${encodeURIComponent(itemId)}`
          : ''),
      coverUrl: coverUrl ? `/api/cover?token=${encodeURIComponent(token)}&id=${encodeURIComponent(itemId)}` : ''
    });
  }

  let nextHref = '';
  let feedLinks = feed.link;
  if (feedLinks) {
    if (!Array.isArray(feedLinks)) feedLinks = [feedLinks];
    const nextLink = feedLinks.find((l: any) => (l['@_rel'] || '').toLowerCase().includes('next') && l['@_href']);
    if (nextLink) {
      try {
        const nextAddress = sameOriginUrl(address, nextLink['@_href']);
        const nextId = crypto.randomUUID().replace(/-/g, '');
        putOpdsSession(token, nextId, {
          username,
          password,
          address,
          download: '',
          cover: '',
          title: '下一页',
          author: '',
          description: '',
          shelfKey: '',
          format: '',
          feed: nextAddress,
          expires: Date.now() + 12 * 60 * 60 * 1000
        });
        nextHref = `/api/opds?token=${encodeURIComponent(token)}&id=${encodeURIComponent(nextId)}`;
      } catch {
        // ignore
      }
    }
  }

  const feedTitle = typeof feed.title === 'string' ? feed.title : (feed.title?.['#text'] || '我的书库');
  return {
    books: entries,
    count: entries.length,
    title: feedTitle,
    backHref: `/api/opds?token=${encodeURIComponent(token)}&id=root`,
    nextHref
  };
}

function pagedHtml(title: string, content: string): string {
  const safeTitle = title.replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c] || c));
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>${safeTitle}</title><style>
  :root{--paper:#f7f5ef;--ink:#30332e;--footer:#f7f5efed;--rule:#deddd5}body[data-theme="white"]{--paper:#fff;--ink:#222;--footer:#fffffff0;--rule:#ddd}body[data-theme="night"]{--paper:#202421;--ink:#e5e3dc;--footer:#202421ed;--rule:#414640}*{box-sizing:border-box}html,body{width:100%;height:100%;margin:0;overflow:hidden}body{background:var(--paper);color:var(--ink);font:18px/2.05 Georgia,"Noto Serif SC",serif;transition:background .2s,color .2s}#pages{position:absolute;inset:0;width:100vw;height:100dvh;overflow-x:auto;overflow-y:hidden;padding:34px 24px 68px;column-width:calc(100vw - 48px);column-gap:48px;column-fill:auto;scroll-behavior:smooth;scroll-snap-type:x mandatory;overscroll-behavior-x:contain;touch-action:pan-y;scrollbar-width:none}#pages::-webkit-scrollbar{display:none}#pages>*{break-inside:avoid-column}.epub-chapter{break-inside:auto}.epub-chapter:not([data-chapter="0"]){break-before:column}h1,h2,h3{font-weight:500;line-height:1.5;margin:0 0 1.2em}h1{font-size:1.45em}h2,h3{margin-top:1.4em}p{margin:0 0 1.2em;text-indent:2em}blockquote{margin:1.4em 0;padding-left:1em;border-left:2px solid #829182;color:inherit;opacity:.8}#page-footer{position:fixed;z-index:2;bottom:0;left:0;right:0;height:52px;padding:0 24px calc(env(safe-area-inset-bottom));display:flex;align-items:center;gap:14px;background:var(--footer);color:var(--ink);opacity:.82;font:11px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;backdrop-filter:blur(12px)}#progress{height:2px;flex:1;background:var(--rule)}#progress i{display:block;width:0;height:100%;background:#778d7d;transition:width .15s}@media(max-width:600px){body{font-size:17px}#pages{padding:28px 22px 64px;column-width:calc(100vw - 44px);column-gap:44px}#page-footer{height:calc(46px + env(safe-area-inset-bottom));padding:0 22px env(safe-area-inset-bottom)}}
  </style></head><body><main id="pages" aria-label="阅读内容"><h1>${safeTitle}</h1>${content}</main><footer id="page-footer"><span id="page-current">1</span><div id="progress"><i></i></div><span id="page-total">1</span></footer><script>
  const pages=document.getElementById('pages'),current=document.getElementById('page-current'),total=document.getElementById('page-total'),bar=document.querySelector('#progress i');let startX=0,startY=0,restoreReady=false,progressFrame=0,lastSentPage=0,lastSentTotal=0;
  function updateProgress(){const width=pages.clientWidth||1,base=Number(window.virtualBasePage)||0;const localPage=Math.min(Math.ceil(pages.scrollWidth/width)||1,Math.floor((pages.scrollLeft+width*.35)/width)+1);const localCount=Math.max(1,Math.ceil(pages.scrollWidth/width));const page=base+localPage,count=base+localCount;const totalKnown=!window.chapterLoadingEnabled||window.allChaptersLoaded;current.textContent=page;total.textContent=totalKnown?count:'…';bar.style.width=(Math.min(page,count)/count*100)+'%';const reportedTotal=totalKnown?count:0;if(restoreReady&&(page!==lastSentPage||reportedTotal!==lastSentTotal)){lastSentPage=page;lastSentTotal=reportedTotal;parent.postMessage({type:'reader-progress',page,total:reportedTotal},location.origin)}}
  function scheduleProgress(){if(progressFrame)return;progressFrame=requestAnimationFrame(()=>{progressFrame=0;updateProgress()})}
  function turn(direction){pages.scrollBy({left:direction*pages.clientWidth,behavior:'smooth'})}
  window.addEventListener('message',event=>{if(event.origin!==location.origin)return;if(event.data?.type==='pages-turn')turn(Math.sign(event.data.direction||0));if(event.data?.type==='reader-restore'){restoreReady=!window.ensureReaderPage;if(window.ensureReaderPage)window.ensureReaderPage(Number(event.data.page||1));else{pages.scrollTo({left:Math.max(0,(Number(event.data.page||1)-1)*pages.clientWidth),behavior:'auto'});requestAnimationFrame(updateProgress)}}if(event.data?.type==='reader-settings'){document.body.dataset.theme=['paper','white','night'].includes(event.data.theme)?event.data.theme:'paper';document.body.style.fontSize=(18*Math.max(.8,Math.min(1.6,Number(event.data.fontScale)||1)))+'px'}});
  pages.addEventListener('touchstart',event=>{if(event.touches.length===1){startX=event.touches[0].clientX;startY=event.touches[0].clientY}},{passive:true});
  pages.addEventListener('touchend',event=>{if(!event.changedTouches.length)return;const dx=event.changedTouches[0].clientX-startX,dy=event.changedTouches[0].clientY-startY;if(Math.abs(dx)>42&&Math.abs(dx)>Math.abs(dy)*1.2)turn(dx<0?1:-1)},{passive:true});
  pages.addEventListener('click',event=>{const x=event.clientX/pages.clientWidth;if(x>.82)turn(1);else if(x<.18)turn(-1)});
  pages.addEventListener('scroll',scheduleProgress,{passive:true});window.addEventListener('resize',scheduleProgress);document.addEventListener('keydown',event=>{if(event.key==='ArrowRight'||event.key==='PageDown')turn(1);if(event.key==='ArrowLeft'||event.key==='PageUp')turn(-1)});requestAnimationFrame(updateProgress);
  </script></body></html>`;
}

function epubReaderHtml(title: string, chapter: string, token: string, itemId: string, count: number): string {
  const safeTitle = title.replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c] || c));
  const content = `<section class="epub-chapter" data-chapter="0"><h1>${safeTitle}</h1>${chapter}</section>`;
  let doc = pagedHtml(title, content);
  doc = doc.replace(`<h1>${safeTitle}</h1>`, '');
  const script = `<script>
  window.chapterLoadingEnabled=true;window.allChaptersLoaded=${count <= 1};
  window.virtualBasePage=0;
  const chapterEndpoint='/api/epub-chapter?token='+encodeURIComponent(${JSON.stringify(token)})+'&id='+encodeURIComponent(${JSON.stringify(itemId)});
  let chapterIndex=1,chapterTotal=${count},chapterLoading=false,restoringReaderPage=false;
  const chapterRanges=[{index:0,node:pages.querySelector('[data-chapter="0"]'),start:1,end:Math.max(1,Math.ceil(pages.scrollWidth/(pages.clientWidth||1)))}];
  function pageCount(){return Math.max(1,Math.ceil(pages.scrollWidth/(pages.clientWidth||1)))}
  function currentPage(){const width=pages.clientWidth||1;return (Number(window.virtualBasePage)||0)+Math.min(pageCount(),Math.floor((pages.scrollLeft+width*.35)/width)+1)}
  function activeChapterIndex(){const page=currentPage();let active=chapterRanges[0]?.index||0;for(const range of chapterRanges){if(page>=range.start)active=range.index;else break}return active}
  async function loadChapter(index){if(chapterLoading)return false;chapterLoading=true;try{const response=await fetch(chapterEndpoint+'&chapter='+index,{cache:'no-store'});const result=await response.json();if(!response.ok)throw new Error(result.error||'章节加载失败');const section=document.createElement('section');section.className='epub-chapter';section.dataset.chapter=String(index);section.innerHTML=result.html||'';pages.append(section);const before=pageCount();chapterRanges.push({index,node:section,start:(Number(window.virtualBasePage)||0)+before+1,end:(Number(window.virtualBasePage)||0)+pageCount()});chapterIndex=index+1;window.allChaptersLoaded=chapterIndex>=chapterTotal;return true}catch(error){parent.postMessage({type:'reader-error',message:error.message||'章节加载失败'},location.origin);return false}finally{chapterLoading=false}}
  async function loadNextChapter(){if(window.allChaptersLoaded)return false;const loaded=await loadChapter(chapterIndex);if(loaded)requestAnimationFrame(()=>{updateProgress();if(!restoringReaderPage){maybeLoadChapter();pruneChapters()}});return loaded}
  function pruneChapters(){if(restoringReaderPage||chapterRanges.length<5)return;const cutoff=activeChapterIndex()-2,remove=chapterRanges.filter(range=>range.index<cutoff);if(!remove.length)return;const width=pages.clientWidth||1,oldCount=pageCount(),oldLeft=pages.scrollLeft;for(const range of remove){range.node.remove()}chapterRanges.splice(0,remove.length);const first=chapterRanges[0]?.node;if(first)first.style.breakBefore='auto';const removedPages=Math.max(0,oldCount-pageCount());window.virtualBasePage=(Number(window.virtualBasePage)||0)+removedPages;pages.scrollLeft=Math.max(0,oldLeft-removedPages*width);requestAnimationFrame(updateProgress)}
  async function resetToStart(){for(const range of chapterRanges)range.node.remove();chapterRanges.length=0;window.virtualBasePage=0;chapterIndex=0;window.allChaptersLoaded=false;await loadChapter(0);requestAnimationFrame(updateProgress)}
  async function ensureReaderPage(page){restoringReaderPage=true;page=Math.max(1,Math.floor(Number(page)||1));if(page<=Number(window.virtualBasePage||0))await resetToStart();while(Number(window.virtualBasePage||0)+pageCount()<page&&!window.allChaptersLoaded){if(!await loadNextChapter())break}const width=pages.clientWidth||1;pages.scrollTo({left:Math.max(0,(page-Number(window.virtualBasePage||0)-1)*width),behavior:'auto'});restoreReady=true;restoringReaderPage=false;requestAnimationFrame(()=>{updateProgress();pruneChapters();maybeLoadChapter()})}
  function maybeLoadChapter(){if(!restoringReaderPage&&!window.allChaptersLoaded&&pages.scrollLeft+pages.clientWidth*2>=pages.scrollWidth)loadNextChapter()}
  pages.addEventListener('scroll',()=>{maybeLoadChapter();pruneChapters()},{passive:true});window.addEventListener('resize',()=>requestAnimationFrame(()=>{updateProgress();maybeLoadChapter()}));requestAnimationFrame(()=>{updateProgress();maybeLoadChapter()});
  </script>`;
  return doc.replace('</body></html>', script + '</body></html>');
}

// EPUB parser helper
const epubSpinesCache = new Map<string, string[]>();
const epubChapterCache = new Map<string, { html: string; count: number }>();

function extractEpubChapter(zipBuffer: Buffer, cacheKey: string, chapterIndex: number): { html: string; count: number } {
  const fullCacheKey = `${cacheKey}:${chapterIndex}`;
  if (epubChapterCache.has(fullCacheKey)) {
    return epubChapterCache.get(fullCacheKey)!;
  }

  const zip = new AdmZip(zipBuffer);
  let spinePaths = epubSpinesCache.get(cacheKey);

  if (!spinePaths) {
    const containerEntry = zip.getEntry('META-INF/container.xml');
    if (!containerEntry) throw new Error('无效的 EPUB 格式 (缺少 container.xml)');
    const containerXml = zip.readAsText(containerEntry);
    const container = xmlParser.parse(containerXml);
    const rootfiles = container.container?.rootfiles?.rootfile;
    const opfPath = Array.isArray(rootfiles) ? rootfiles[0]['@_full-path'] : rootfiles?.['@_full-path'];
    if (!opfPath) throw new Error('无效的 EPUB 格式 (缺少 rootfile)');

    const opfEntry = zip.getEntry(opfPath);
    if (!opfEntry) throw new Error('找不到 OPF 元数据文件');
    const opfXml = zip.readAsText(opfEntry);
    const opf = xmlParser.parse(opfXml);

    const baseDir = path.dirname(opfPath);
    const manifestItems = opf.package?.manifest?.item;
    const manifestMap = new Map<string, string>();
    const items = Array.isArray(manifestItems) ? manifestItems : [manifestItems];
    for (const item of items) {
      if (item && item['@_id'] && item['@_href']) {
        const href = item['@_href'];
        const fullHref = baseDir ? path.posix.join(baseDir, href) : href;
        manifestMap.set(item['@_id'], fullHref);
      }
    }

    const spineItems = opf.package?.spine?.itemref;
    const itemrefs = Array.isArray(spineItems) ? spineItems : [spineItems];
    spinePaths = [];
    for (const ref of itemrefs) {
      if (ref && ref['@_idref']) {
        const resolved = manifestMap.get(ref['@_idref']);
        if (resolved) spinePaths.push(resolved);
      }
    }
    epubSpinesCache.set(cacheKey, spinePaths);
  }

  const totalChapters = spinePaths.length;
  if (chapterIndex < 0 || chapterIndex >= totalChapters) {
    return { html: '', count: totalChapters };
  }

  const targetPath = spinePaths[chapterIndex];
  const entry = zip.getEntry(targetPath);
  if (!entry) {
    return { html: '', count: totalChapters };
  }

  const rawHtml = zip.readAsText(entry);
  // Clean raw HTML: extract body and strip scripts
  let bodyContent = rawHtml;
  const bodyMatch = rawHtml.match(/<body[^>]*>([\s\S]*?)<\/body>/i);
  if (bodyMatch) {
    bodyContent = bodyMatch[1];
  }
  bodyContent = bodyContent.replace(/<script\b[^<]*(?:(?!<\/script>)<[^<]*)*<\/script>/gi, '');
  bodyContent = bodyContent.replace(/<style\b[^<]*(?:(?!<\/style>)<[^<]*)*<\/style>/gi, '');

  const result = { html: bodyContent, count: totalChapters };
  epubChapterCache.set(fullCacheKey, result);
  return result;
}

// Express App
const app = express();
app.use(express.json({ limit: '2mb' }));

// Health
app.get('/health', (_req: Request, res: Response) => {
  res.type('text/plain').send('ok');
});

// Auth helper
function getAuthToken(req: Request): string {
  const cookie = req.headers.cookie || '';
  const match = cookie.match(/pages_session=([^;]+)/);
  return match ? match[1] : '';
}

function checkAuth(req: Request): boolean {
  if (!APP_PASSWORD) return true;
  const token = getAuthToken(req);
  if (!token) return false;
  const expiry = authSessions.get(token) || 0;
  return expiry > Date.now();
}

// Auth endpoints
app.get('/api/auth/status', (req: Request, res: Response) => {
  const authenticated = checkAuth(req);
  res.json({
    required: Boolean(APP_PASSWORD),
    authenticated
  });
});

app.post('/api/login', (req: Request, res: Response) => {
  if (!APP_PASSWORD) {
    res.json({ ok: true, required: false });
    return;
  }
  const password = String(req.body.password || '');
  if (password !== APP_PASSWORD) {
    res.status(401).json({ error: '阅读器密码不正确。' });
    return;
  }
  const token = crypto.randomBytes(32).toString('hex');
  authSessions.set(token, Date.now() + 12 * 60 * 60 * 1000);
  let cookie = `pages_session=${token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=43200`;
  if (COOKIE_SECURE) cookie += '; Secure';
  res.setHeader('Set-Cookie', cookie);
  res.json({ ok: true });
});

app.post('/api/logout', (req: Request, res: Response) => {
  const token = getAuthToken(req);
  if (token) authSessions.delete(token);
  let cookie = `pages_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0`;
  if (COOKIE_SECURE) cookie += '; Secure';
  res.setHeader('Set-Cookie', cookie);
  res.json({ ok: true });
});

// Middleware for protected API endpoints
app.use('/api', (req: Request, res: Response, next: NextFunction) => {
  if (req.path === '/auth/status' || req.path === '/login' || req.path === '/logout') {
    return next();
  }
  if (!checkAuth(req)) {
    res.status(401).json({ error: '请先登录阅读器。', loginRequired: true });
    return;
  }
  next();
});

// Restore saved library
app.get('/api/restore', async (_req: Request, res: Response) => {
  try {
    const config = loadConnectionConfig();
    if (!config || !config.address) {
      res.status(404).json({ error: '尚未保存书库连接。' });
      return;
    }
    const { address, username = '', password = '' } = config;
    const remoteRes = await fetchRemote(address, username, password, 'application/atom+xml, application/xml, text/xml');
    if (!remoteRes.ok) {
      res.status(remoteRes.status).json({ error: `连接书库失败 (HTTP ${remoteRes.status})` });
      return;
    }
    const xml = await remoteRes.text();
    const token = crypto.randomBytes(24).toString('hex');
    putOpdsSession(token, 'root', {
      username,
      password,
      feed: address,
      expires: Date.now() + 12 * 60 * 60 * 1000
    });
    const parsed = parseOpdsFeed(xml, address, username, password, token);
    res.json({ ...parsed, token, address });
  } catch (err: any) {
    res.status(502).json({ error: err.message || '无法恢复已保存的书库连接。' });
  }
});

// Connect to OPDS
app.post('/api/opds', async (req: Request, res: Response) => {
  try {
    const address = String(req.body.address || '').trim();
    const username = String(req.body.username || '');
    const password = String(req.body.password || '');
    const persist = req.body.persist !== false;

    if (!address) {
      res.status(400).json({ error: '请输入有效的书库地址。' });
      return;
    }

    const remoteRes = await fetchRemote(address, username, password, 'application/atom+xml, application/xml, text/xml');
    if (!remoteRes.ok) {
      res.status(remoteRes.status).json({ error: `Calibre-Web 返回 HTTP ${remoteRes.status}，请检查账号和地址。` });
      return;
    }

    const xml = await remoteRes.text();
    const token = crypto.randomBytes(24).toString('hex');
    putOpdsSession(token, 'root', {
      username,
      password,
      feed: address,
      expires: Date.now() + 12 * 60 * 60 * 1000
    });

    const parsed = parseOpdsFeed(xml, address, username, password, token);
    if (persist) {
      saveConnectionConfig(address, username, password);
    } else {
      deleteConnectionConfig();
    }
    res.json({ ...parsed, token, address });
  } catch (err: any) {
    res.status(502).json({ error: err.message || '无法连接书库。请检查地址、网络和服务器状态。' });
  }
});

// Load OPDS sub-category or paginated page
app.get('/api/opds', async (req: Request, res: Response) => {
  try {
    const token = String(req.query.token || '');
    const id = String(req.query.id || '');
    const item = getOpdsSession(token, id);
    if (!item || !item.feed) {
      res.status(404).json({ error: '分类或页面链接已失效，请重新连接书库。' });
      return;
    }
    const remoteRes = await fetchRemote(item.feed, item.username || '', item.password || '', 'application/atom+xml, application/xml, text/xml');
    if (!remoteRes.ok) {
      res.status(remoteRes.status).json({ error: `读取分类失败 (HTTP ${remoteRes.status})` });
      return;
    }
    const xml = await remoteRes.text();
    const parsed = parseOpdsFeed(xml, item.feed, item.username || '', item.password || '', token);
    res.json(parsed);
  } catch (err: any) {
    res.status(502).json({ error: err.message || '无法读取分类目录。' });
  }
});

// Shelf endpoints
app.get('/api/shelf', (req: Request, res: Response) => {
  const token = String(req.query.token || '');
  const root = getOpdsSession(token, 'root');
  if (!root) {
    res.status(403).json({ error: '书库会话已过期，请重新连接。' });
    return;
  }
  const acceptedIds = libraryIds(root.feed || '');
  const records = loadBookshelf();
  const result: any[] = [];
  for (const record of records) {
    if (!record || !acceptedIds.has(record.library)) continue;
    const itemId = record.key;
    putOpdsSession(token, itemId, {
      username: root.username,
      password: root.password,
      address: root.feed,
      download: record.download,
      cover: record.cover,
      title: record.title || '未命名书籍',
      author: record.author || '未知作者',
      description: record.description || '',
      shelfKey: itemId,
      format: record.format || '',
      expires: root.expires
    });
    const query = `token=${encodeURIComponent(token)}&id=${encodeURIComponent(itemId)}`;
    result.push({
      id: itemId,
      key: itemId,
      shelfKey: itemId,
      title: record.title || '未命名书籍',
      author: record.author || '未知作者',
      description: record.description || '',
      tag: (record.format || '书架').toUpperCase(),
      category: 'shelf',
      kind: 'book',
      format: record.format || '',
      href: `/api/read?${query}`,
      coverUrl: record.cover ? `/api/cover?${query}` : ''
    });
  }
  res.json({ books: result, count: result.length });
});

app.post('/api/shelf', (req: Request, res: Response) => {
  const token = String(req.body.token || '');
  const itemId = String(req.body.id || '');
  const action = String(req.body.action || '');
  const root = getOpdsSession(token, 'root');
  if (!root) {
    res.status(403).json({ error: '书库会话已过期，请重新连接。' });
    return;
  }

  const acceptedIds = libraryIds(root.feed || '');
  let records = loadBookshelf();
  const item = getOpdsSession(token, itemId);

  if (action === 'add') {
    if (!item || !item.download || !item.shelfKey) {
      res.status(404).json({ error: '找不到这本书，请刷新书库后重试。' });
      return;
    }
    const key = item.shelfKey;
    records = records.filter(r => !(r.key === key && acceptedIds.has(r.library)));
    records.push({
      key,
      library: libraryIdentity(root.feed || ''),
      title: item.title,
      author: item.author,
      description: item.description || '',
      format: item.format || '',
      download: item.download,
      cover: item.cover || ''
    });
    saveBookshelf(records);
  } else if (action === 'remove') {
    const stableKey = item?.shelfKey || itemId;
    records = records.filter(r => !(r.key === stableKey && acceptedIds.has(r.library)));
    saveBookshelf(records);
  } else {
    res.status(400).json({ error: '书架操作无效。' });
    return;
  }
  res.json({ ok: true });
});

// Reading & Browse History
app.get('/api/history', (req: Request, res: Response) => {
  const token = String(req.query.token || '');
  const view = req.query.type === 'browse' ? 'browse' : 'read';
  const root = getOpdsSession(token, 'root');
  if (!root) {
    res.status(403).json({ error: '书库会话已过期，请重新连接。' });
    return;
  }

  const acceptedIds = libraryIds(root.feed || '');
  const timeField = view === 'browse' ? 'lastViewed' : 'lastRead';
  const records = loadReadingHistory();
  const result: any[] = [];

  for (const record of records) {
    if (!acceptedIds.has(record.library) || !record[timeField]) continue;
    const itemId = record.key;
    putOpdsSession(token, itemId, {
      username: root.username,
      password: root.password,
      address: root.feed,
      download: record.download,
      cover: record.cover,
      title: record.title || '未命名书籍',
      author: record.author || '未知作者',
      description: record.description || '',
      shelfKey: itemId,
      format: record.format || '',
      expires: root.expires
    });
    const query = `token=${encodeURIComponent(token)}&id=${encodeURIComponent(itemId)}`;
    result.push({
      ...record,
      id: itemId,
      shelfKey: itemId,
      kind: 'book',
      category: 'history',
      href: `/api/read?${query}`,
      coverUrl: record.cover ? `/api/cover?${query}` : ''
    });
  }

  result.sort((a, b) => (b[timeField] || 0) - (a[timeField] || 0));
  res.json({ books: result.slice(0, 100), count: Math.min(result.length, 100) });
});

app.post('/api/history', (req: Request, res: Response) => {
  const token = String(req.body.token || '');
  const itemId = String(req.body.id || '');
  const action = req.body.action === 'browse' ? 'browse' : 'read';
  const root = getOpdsSession(token, 'root');
  const item = getOpdsSession(token, itemId);

  if (!root || !item || !item.download) {
    res.status(403).json({ error: '阅读会话已过期，请重新连接书库。' });
    return;
  }

  const key = item.shelfKey || shelfKey(item.download, item.title || '', item.author || '');
  const now = Math.floor(Date.now() / 1000);
  let records = loadReadingHistory();
  const prevIndex = records.findIndex(r => r.key === key && r.library === libraryIdentity(root.feed || ''));
  const prev = prevIndex >= 0 ? records[prevIndex] : null;

  const record = {
    key,
    library: libraryIdentity(root.feed || ''),
    title: item.title || '未命名书籍',
    author: item.author || '未知作者',
    description: item.description || '',
    format: item.format || '',
    download: item.download,
    cover: item.cover || '',
    lastRead: prev ? Number(prev.lastRead || 0) : 0,
    lastViewed: now
  };

  if (action === 'read') {
    record.lastRead = now;
  }

  records = records.filter(r => !(r.key === key && r.library === record.library));
  records.push(record);
  records.sort((a, b) => (b.lastRead || 0) - (a.lastRead || 0));
  saveReadingHistory(records.slice(0, 500));
  res.json({ ok: true });
});

// Recommendations
app.get('/api/recommendations', async (req: Request, res: Response) => {
  const token = String(req.query.token || '');
  const root = getOpdsSession(token, 'root');
  if (!root) {
    res.status(403).json({ error: '书库会话已过期，请重新连接。' });
    return;
  }

  const cached = recommendationCache.get(token);
  if (cached && cached.expires > Date.now()) {
    res.json({ books: cached.books, count: cached.books.length });
    return;
  }

  try {
    const remoteRes = await fetchRemote(root.feed || '', root.username || '', root.password || '');
    if (remoteRes.ok) {
      const xml = await remoteRes.text();
      const parsed = parseOpdsFeed(xml, root.feed || '', root.username || '', root.password || '', token);
      const candidates = parsed.books.filter(b => b.kind === 'book');
      recommendationCache.set(token, { expires: Date.now() + 3600000, books: candidates });
      res.json({ books: candidates, count: candidates.length });
      return;
    }
  } catch {
    // fallback
  }

  res.json({ books: [], count: 0 });
});

// Cover image proxy
app.get('/api/cover', async (req: Request, res: Response) => {
  const token = String(req.query.token || '');
  const itemId = String(req.query.id || '');
  const item = getOpdsSession(token, itemId);
  if (!item || !item.cover) {
    res.redirect('/cover-placeholder.svg');
    return;
  }
  try {
    const remoteRes = await fetchRemote(item.cover, item.username || '', item.password || '');
    if (!remoteRes.ok) {
      res.redirect('/cover-placeholder.svg');
      return;
    }
    const contentType = remoteRes.headers.get('content-type') || 'image/jpeg';
    res.setHeader('Content-Type', contentType);
    res.setHeader('Cache-Control', 'private, max-age=86400');
    const buffer = Buffer.from(await remoteRes.arrayBuffer());
    res.send(buffer);
  } catch {
    res.redirect('/cover-placeholder.svg');
  }
});

// EPUB Chapter loader
const epubBufferCache = new Map<string, Buffer>();

app.get('/api/epub-chapter', async (req: Request, res: Response) => {
  const token = String(req.query.token || '');
  const itemId = String(req.query.id || '');
  const chapterIndex = parseInt(String(req.query.chapter || '0'), 10);
  const item = getOpdsSession(token, itemId);

  if (!item || !item.download) {
    res.status(403).json({ error: '书籍链接已失效。' });
    return;
  }

  try {
    const cacheKey = `${token}:${itemId}`;
    let buffer = epubBufferCache.get(cacheKey);
    if (!buffer) {
      const remoteRes = await fetchRemote(item.download, item.username || '', item.password || '');
      if (!remoteRes.ok) throw new Error('下载 EPUB 章节失败');
      buffer = Buffer.from(await remoteRes.arrayBuffer());
      epubBufferCache.set(cacheKey, buffer);
    }

    const { html, count } = extractEpubChapter(buffer, cacheKey, chapterIndex);
    res.json({ html, chapter: chapterIndex, count });
  } catch (err: any) {
    res.status(400).json({ error: err.message || '无法读取 EPUB 章节。' });
  }
});

// Read endpoint
app.get('/api/read', async (req: Request, res: Response) => {
  const token = String(req.query.token || '');
  const itemId = String(req.query.id || '');
  const item = getOpdsSession(token, itemId);

  if (!item || !item.download) {
    res.status(404).send('书籍不存在或链接已失效。');
    return;
  }

  const format = (item.format || '').toLowerCase();
  try {
    if (format === 'epub') {
      const cacheKey = `${token}:${itemId}`;
      let buffer = epubBufferCache.get(cacheKey);
      if (!buffer) {
        const remoteRes = await fetchRemote(item.download, item.username || '', item.password || '');
        if (!remoteRes.ok) throw new Error('下载电子书失败');
        buffer = Buffer.from(await remoteRes.arrayBuffer());
        epubBufferCache.set(cacheKey, buffer);
      }
      const { html: firstChapter, count } = extractEpubChapter(buffer, cacheKey, 0);
      const readerHtml = epubReaderHtml(item.title || '电子书', firstChapter, token, itemId, count);
      res.setHeader('Content-Type', 'text/html; charset=utf-8');
      res.send(readerHtml);
      return;
    }

    if (format === 'pdf') {
      const remoteRes = await fetchRemote(item.download, item.username || '', item.password || '');
      if (!remoteRes.ok) throw new Error('下载 PDF 失败');
      const buffer = Buffer.from(await remoteRes.arrayBuffer());
      res.setHeader('Content-Type', 'application/pdf');
      res.setHeader('Content-Disposition', 'inline');
      res.send(buffer);
      return;
    }

    // Default: TXT or plain
    const remoteRes = await fetchRemote(item.download, item.username || '', item.password || '');
    const text = await remoteRes.text();
    const formatted = text.split('\n').filter(Boolean).map(p => `<p>${p.trim()}</p>`).join('');
    const html = pagedHtml(item.title || '电子书', formatted);
    res.setHeader('Content-Type', 'text/html; charset=utf-8');
    res.send(html);
  } catch (err: any) {
    res.status(502).send(`<!doctype html><body style="font:16px sans-serif;padding:2em;color:#555;background:#f7f5ef">无法打开这本书: ${err.message || ''}</body>`);
  }
});

// Stream raw book
app.get('/api/book', async (req: Request, res: Response) => {
  const token = String(req.query.token || '');
  const itemId = String(req.query.id || '');
  const item = getOpdsSession(token, itemId);
  if (!item || !item.download) {
    res.status(404).send('Not found');
    return;
  }
  try {
    const remoteRes = await fetchRemote(item.download, item.username || '', item.password || '');
    if (!remoteRes.ok) {
      res.status(remoteRes.status).send('Could not fetch book');
      return;
    }
    const contentType = remoteRes.headers.get('content-type') || 'application/octet-stream';
    res.setHeader('Content-Type', contentType);
    const buffer = Buffer.from(await remoteRes.arrayBuffer());
    res.send(buffer);
  } catch (err: any) {
    res.status(502).send(err.message || 'Error');
  }
});

// Static files with no-cache for index.html, root, and sw.js
app.use((req: Request, res: Response, next: NextFunction) => {
  if (req.path === '/' || req.path.endsWith('.html') || req.path === '/sw.js') {
    res.setHeader('Cache-Control', 'no-cache, no-store, must-revalidate');
    res.setHeader('Pragma', 'no-cache');
    res.setHeader('Expires', '0');
  }
  next();
});

app.use(express.static(ROOT, {
  index: 'index.html',
  maxAge: 0,
  setHeaders: (res: Response, filePath: string) => {
    if (filePath.endsWith('.html') || filePath.endsWith('sw.js')) {
      res.setHeader('Cache-Control', 'no-cache, no-store, must-revalidate');
      res.setHeader('Pragma', 'no-cache');
      res.setHeader('Expires', '0');
    }
  }
}));

// Fallback for root
app.get('/', (_req: Request, res: Response) => {
  res.setHeader('Cache-Control', 'no-cache, no-store, must-revalidate');
  res.sendFile(path.join(ROOT, 'index.html'));
});

app.listen(PORT, HOST, () => {
  console.log(`页间阅读器 running on http://${HOST}:${PORT}`);
});
