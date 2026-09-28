// Vite changes Host for the upstream, but leaves the browser's Origin unchanged.
// Translate only same-origin loopback requests; never launder foreign origins.
export function devApiProxy(target) {
  return {
    target,
    changeOrigin: true,
    configure(proxy) {
      proxy.on('proxyReq', (upstream, request) => {
        const host = request.headers.host;
        if (/^(127\.0\.0\.1|localhost|\[::1\]):\d+$/.test(host || '') &&
            request.headers.origin === `http://${host}`) {
          upstream.setHeader('Origin', new URL(target).origin);
        }
      });
    },
  };
}
