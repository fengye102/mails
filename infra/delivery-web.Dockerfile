FROM node:22-alpine AS build
WORKDIR /app
COPY apps/delivery_web/package.json apps/delivery_web/package-lock.json ./
RUN npm ci
COPY apps/delivery_web/ ./
RUN npm run build

FROM caddy:2-alpine
COPY infra/delivery-web.Caddyfile /etc/caddy/Caddyfile
COPY --from=build /app/dist /srv
