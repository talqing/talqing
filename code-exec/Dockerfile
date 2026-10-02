# node 22 + isolated-vm 7 — isolated-vm 5 segfaults on arm64 Linux (Node 20)
FROM node:22-bookworm-slim

ENV NODE_ENV=production

# isolated-vm is a native module — node-gyp needs python3 + a C++ toolchain
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        make \
        g++ \
        ca-certificates \
        wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm install --omit=dev && npm cache clean --force

COPY . .

# no platform credentials reach this container; drop root so a sandbox escape
# lands in an unprivileged process on a creds-free box
USER node

EXPOSE 8090
CMD ["node", "server.js"]
