FROM node:24-alpine AS build

WORKDIR /web

COPY package.json package-lock.json ./
# 用国内镜像源加速；用 npm install 而非 npm ci：
# package-lock.json 在 Windows 上生成，缺少 Linux(musl) 平台的可选原生依赖(@emnapi 等)，
# npm ci 严格校验会因锁文件不同步而失败，npm install 会在构建平台自动补齐。
RUN npm config set registry https://registry.npmmirror.com \
    && npm config set fetch-retries 5 \
    && npm config set fetch-retry-maxtimeout 120000 \
    && npm install --no-audit --no-fund

COPY . .
RUN npm run build

FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /web/dist /usr/share/nginx/html

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
