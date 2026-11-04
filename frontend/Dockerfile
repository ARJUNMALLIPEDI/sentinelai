# Development image: runs the Vite dev server with hot reload.
# A production stage (static build served by nginx) is added in Milestone 7.
FROM node:22-alpine
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
EXPOSE 5173
CMD ["npx", "vite", "--host", "0.0.0.0", "--port", "5173"]
