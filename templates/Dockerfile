# 通用 Node.js 应用 Dockerfile
# 根据实际项目类型修改

FROM node:20-alpine AS builder

WORKDIR /app

# 安装依赖
COPY package*.json ./
RUN npm ci --production=false

# 构建
COPY . .
RUN npm run build

# 生产镜像
FROM node:20-alpine AS production

WORKDIR /app

# 只安装生产依赖
COPY package*.json ./
RUN npm ci --production=true

# 从 builder 复制构建产物
COPY --from=builder /app/dist ./dist

# 非 root 运行
RUN addgroup -g 1001 -S appgroup && \
    adduser -S appuser -G appgroup -u 1001
USER appuser

EXPOSE 3000

CMD ["node", "dist/index.js"]
