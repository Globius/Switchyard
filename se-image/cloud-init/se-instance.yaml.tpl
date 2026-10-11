#cloud-config
# Per-SE user-data snippet the controller attaches with cicustom (user=...). Placeholders are filled by
# the deploy job; the enrollment token is single use and expires (ADR 0011).
write_files:
  - path: /etc/switchyard/agent/enroll.env
    permissions: "0600"
    content: |
      CONTROLLER_URL=${controller_url}
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
runcmd:
  - [systemctl, restart, sy-agent-enroll.service]
  - [systemctl, restart, vector.service]
