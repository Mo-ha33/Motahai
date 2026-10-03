# MCP capability-URL routing for api.motahai.com — rendered on the VPS, NEVER committed.
# Placeholders, filled on the VPS by sed (see deploy/README.md):
#   SECRET    64 hex chars from ~/.hermes/secrets/mcp_url_secret
#   UPSTREAM  the exact proxy_pass target of your CURRENT /mcp/ block (from `nginx -T`),
#             e.g. http://172.17.0.1:58775  (no path, no trailing slash)

# The secret path. Longest-prefix match wins over the plain /mcp/ block below.
location /mcp/__SECRET__/ {
    access_log off;                                   # the URL is a credential — keep it out of logs
    client_max_body_size 256k;

    proxy_set_header X-Forwarded-Prefix /mcp/__SECRET__;   # app builds the SSE `endpoint` URL from this
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    proxy_pass __UPSTREAM__/mcp/;                     # trailing slash: /mcp/<secret>/sse → /mcp/sse upstream
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_buffering off;
    proxy_cache off;
    gzip off;
    add_header X-Accel-Buffering no;
    proxy_read_timeout 330s;
    proxy_send_timeout 330s;
}

# __OLD_MCP_BLOCK__
# Phase 1 (until Wesam is switched): your existing `location /mcp/ { ... }` block, copied verbatim.
# Phase 2 (after the switch): replace it with:
#   location /mcp/ { return 404; }
