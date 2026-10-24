# Use the TCP peer, never a client-supplied X-Forwarded-For header.
# Restrict the privileged judge API on the private TLS gateway.
location ^~ /api/internal/judge/editor-tasks/ {
    allow 10.10.10.111;
    allow 10.10.10.112;
    allow 10.10.10.113;
    allow 10.10.10.114;
    allow 10.10.10.115;
    allow 10.10.10.116;
    allow 10.10.10.117;
    deny all;
    client_max_body_size 768m;
    proxy_request_buffering off;
    proxy_read_timeout 150s;
    proxy_pass http://api_backend;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
}

# Use the TCP peer, never a client-supplied X-Forwarded-For header.
# Restrict the privileged judge API on the private TLS gateway.
location ^~ /api/internal/judge/ {
    allow 10.10.10.111;
    allow 10.10.10.112;
    allow 10.10.10.113;
    allow 10.10.10.114;
    allow 10.10.10.115;
    allow 10.10.10.116;
    allow 10.10.10.117;
    deny all;
    # Single-input reference reports retain up to 128 MiB of stdout.
    # Allow its base64 encoding and metadata; application validation keeps
    # ordinary verification reports at the existing 16 MiB aggregate budget.
    client_max_body_size 192m;
    proxy_read_timeout 150s;
    proxy_pass http://api_backend;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
}
