FROM node:22-alpine AS build

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

ARG VITE_API_URL=/api
ARG VITE_FOUNDRY_URL=http://localhost:30000
ENV VITE_API_URL=${VITE_API_URL} \
    VITE_FOUNDRY_URL=${VITE_FOUNDRY_URL}

COPY . ./
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.31-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
