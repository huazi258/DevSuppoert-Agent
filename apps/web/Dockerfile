FROM node:22-alpine

WORKDIR /app

COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci

COPY apps/web/ ./

ARG NEXT_PUBLIC_DEVSUPPORT_API_BASE_URL=/api
ARG DEVSUPPORT_BACKEND_API_URL=http://backend:8000
ENV NEXT_PUBLIC_DEVSUPPORT_API_BASE_URL=$NEXT_PUBLIC_DEVSUPPORT_API_BASE_URL \
    DEVSUPPORT_BACKEND_API_URL=$DEVSUPPORT_BACKEND_API_URL \
    NODE_ENV=production

RUN npm run build

EXPOSE 3000

CMD ["npm", "run", "start", "--", "-H", "0.0.0.0", "-p", "3000"]
