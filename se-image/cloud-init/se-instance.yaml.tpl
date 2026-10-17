#cloud-config
# Per-SE user-data snippet the controller attaches with cicustom (user=...). Placeholders are filled by
# the deploy job; the enrollment token is single use and expires (ADR 0011). controller_url MUST be https://:
# the agent refuses http (the answer carries the CA and the bundle key); `--insecure-plain-http` is for labs only.
write_files:
  - path: /etc/switchyard/agent/enroll.env
    permissions: "0600"
    content: |
      CONTROLLER_URL=${controller_url}
      CONTROLLER_CN=${controller_cn}
      DP_USER=${dataplane_user}
      DP_PASSWORD=${dataplane_password}
  - path: /etc/switchyard/agent/enroll.token
    permissions: "0600"
    content: "${enroll_token}\n"
  - path: /etc/switchyard/agent/controller-ca.crt
    permissions: "0644"
    content: |
      ${controller_ca_indented}
  - path: /etc/switchyard/vector.env
    permissions: "0644"
    content: |
      LOKI_URL=${loki_url}
      TENANT_ID=${tenant_id}
      TENANT_NAME=${tenant_name}
      SE_GROUP=${se_group}
      ENGINE=${engine}
      # Optional second log sink (ADR 0221): OTLP/HTTP logs. Unset, only Loki is fed. Loki is optional too: leave
      # LOKI_URL empty for an OTLP-only engine.
      # OTLP_LOGS_URL=https://collector.example.org:4318
      # OTLP_LOGS_HEADERS=Authorization: Bearer <token>
      # OTLP_LOGS_INSECURE_SKIP_VERIFY=false
      # OTLP_LOGS_CA_FILE=/etc/switchyard/otlp-ca.pem
runcmd:
  - [systemctl, restart, sy-agent-enroll.service]
  - [systemctl, restart, vector.service]
