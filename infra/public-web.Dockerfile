FROM node:22-alpine AS build
WORKDIR /app
COPY apps/public_web/package.json apps/public_web/package-lock.json ./
RUN npm ci
COPY apps/public_web/ ./
RUN npm run build

FROM caddy:2-alpine
COPY infra/public-web.Caddyfile /etc/caddy/Caddyfile
COPY --from=build /app/dist /srv