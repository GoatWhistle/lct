FROM node:20-slim AS build

WORKDIR /app
COPY package.json package-lock.json* .npmrc ./
RUN npm install

COPY . .
RUN npm run build

FROM nginx:1.27-alpine

RUN apk add --no-cache openssl
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY nginx.tls.conf.template /etc/nginx/templates-tls/default.conf.template
COPY docker-entrypoint.d/15-local-certificate.sh /docker-entrypoint.d/15-local-certificate.sh
COPY --from=build /app/dist /usr/share/nginx/html

ENV BACKEND_HOST=backend
ENV BACKEND_PORT=8000
EXPOSE 5173
EXPOSE 5443
