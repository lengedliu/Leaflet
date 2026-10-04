FROM node:22-alpine

ENV PORT=3000
WORKDIR /app

# 安装依赖
COPY package*.json ./
RUN npm install

# 复制代码
COPY . .

# 配置文件持久化挂载点
VOLUME ["/app/config"]

EXPOSE 3000

CMD ["npm", "run", "dev"]
