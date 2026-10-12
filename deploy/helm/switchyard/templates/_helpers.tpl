{{- define "sy.name" -}}{{ .Release.Name }}{{- end -}}
{{- define "sy.labels" -}}
app.kubernetes.io/name: switchyard
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}
{{- define "sy.selector" -}}
app.kubernetes.io/name: switchyard
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}
{{- define "sy.controllerImage" -}}{{ .Values.image.controller.repository }}:{{ .Values.image.controller.tag }}{{- end -}}

{{/* Database: host, port, and the secret holding the user and password. */}}
{{- define "sy.dbHost" -}}
{{- if eq .Values.database.mode "cnpg" }}{{ .Release.Name }}-db-rw{{ else }}{{ required "database.external.host is required" .Values.database.external.host }}{{ end -}}
{{- end -}}
{{- define "sy.dbPort" -}}{{ if eq .Values.database.mode "cnpg" }}5432{{ else }}{{ .Values.database.external.port }}{{ end }}{{- end -}}
{{- define "sy.dbSecret" -}}
{{- if eq .Values.database.mode "cnpg" }}{{ .Release.Name }}-db-superuser{{ else }}{{ required "database.external.existingSecret is required" .Values.database.external.existingSecret }}{{ end -}}
{{- end -}}
{{- define "sy.dbUserKey" -}}{{ if eq .Values.database.mode "cnpg" }}username{{ else }}{{ .Values.database.external.userKey }}{{ end }}{{- end -}}
{{- define "sy.dbPasswordKey" -}}{{ if eq .Values.database.mode "cnpg" }}password{{ else }}{{ .Values.database.external.passwordKey }}{{ end }}{{- end -}}
{{- define "sy.dbOptions" -}}{{ if eq .Values.database.mode "cnpg" }}{{ else }}{{ .Values.database.external.options }}{{ end }}{{- end -}}

{{/* The database environment; DB_USER and DB_PASSWORD come first because the URL refers to them. */}}
{{- define "sy.dbEnv" -}}
- name: DB_USER
  valueFrom: {secretKeyRef: {name: {{ include "sy.dbSecret" . }}, key: {{ include "sy.dbUserKey" . }}}}
- name: DB_PASSWORD
  valueFrom: {secretKeyRef: {name: {{ include "sy.dbSecret" . }}, key: {{ include "sy.dbPasswordKey" . }}}}
- name: SWITCHYARD_DATABASE_URL
  value: "postgresql+psycopg://$(DB_USER):$(DB_PASSWORD)@{{ include "sy.dbHost" . }}:{{ include "sy.dbPort" . }}/{{ .Values.database.name }}{{ include "sy.dbOptions" . }}"
{{- end -}}

{{/* Environment shared by the API, the worker and the migration job. */}}
{{- define "sy.controllerEnv" -}}
{{ include "sy.dbEnv" . }}
- name: SWITCHYARD_PUBLIC_URL
  value: {{ required "publicUrl is required" .Values.publicUrl | quote }}
- name: SWITCHYARD_TRUST_FORWARDED
  value: {{ ternary "1" "0" .Values.controller.trustForwarded | quote }}
- name: SWITCHYARD_METRICS_TOKEN
  valueFrom: {secretKeyRef: {name: {{ required "secrets.existingSecret is required" .Values.secrets.existingSecret }}, key: metrics-token, optional: true}}
- name: SWITCHYARD_OPENBAO_ADDR
  value: {{ required "openbao.addr is required" .Values.openbao.addr | quote }}
- name: SWITCHYARD_OPENBAO_ROLE_ID
  valueFrom: {secretKeyRef: {name: {{ required "openbao.existingSecret is required" .Values.openbao.existingSecret }}, key: {{ .Values.openbao.roleIdKey }}}}
- name: SWITCHYARD_OPENBAO_SECRET_ID
  valueFrom: {secretKeyRef: {name: {{ .Values.openbao.existingSecret }}, key: {{ .Values.openbao.secretIdKey }}}}
- name: SWITCHYARD_PKI_MOUNT
  value: {{ .Values.openbao.pkiMount | quote }}
- name: SWITCHYARD_PKI_ROLE
  value: {{ .Values.openbao.pkiRole | quote }}
- name: SWITCHYARD_SHARED_SECRET_PREFIXES
  value: {{ .Values.openbao.sharedSecretPrefixes | quote }}
- name: SWITCHYARD_BUNDLE_KEY_FILE
  value: /secrets/bundle.key
- name: SWITCHYARD_DATAPLANE_CA
  value: /secrets/ca.pem
- name: SWITCHYARD_DATAPLANE_CERT
  value: /secrets/client.crt
- name: SWITCHYARD_DATAPLANE_KEY
  value: /secrets/client.key
- name: SWITCHYARD_BREAK_GLASS
  value: {{ ternary "1" "0" .Values.controller.breakGlass | quote }}
- name: SWITCHYARD_ALLOW_LXC
  value: {{ ternary "1" "0" .Values.controller.allowLxc | quote }}
{{- with .Values.controller.engineCidrs }}
- name: SWITCHYARD_ENGINE_CIDRS
  value: {{ . | quote }}
{{- end }}
{{- with .Values.controller.proxmox }}
{{- if .url }}
- name: SWITCHYARD_PVE_URL
  value: {{ .url | quote }}
- name: SWITCHYARD_PVE_TOKEN_ID
  value: {{ required "controller.proxmox.tokenId is required" .tokenId | quote }}
- name: SWITCHYARD_PVE_CA
  value: /secrets/{{ .caKey }}
- name: SWITCHYARD_SECRET_KV_PROXMOX_TOKEN
  valueFrom: {secretKeyRef: {name: {{ required "controller.proxmox.tokenSecret.existingSecret is required" .tokenSecret.existingSecret }}, key: {{ .tokenSecret.key }}}}
{{- end }}
{{- end }}
{{- with .Values.controller.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{- define "sy.secretsVolume" -}}
- name: secrets
  secret: {secretName: {{ .Values.secrets.existingSecret }}, defaultMode: 0440}
- name: tmp
  emptyDir: {}
{{- end -}}

{{/* Waits until the database is reachable and migrated to this image's head revision. */}}
{{- define "sy.waitSchema" -}}
- name: wait-schema
  image: {{ include "sy.controllerImage" . }}
  imagePullPolicy: {{ .Values.image.controller.pullPolicy }}
  command: ["python", "-c"]
  args:
    - |
      import os, sys, time
      from alembic.script import ScriptDirectory
      from sqlalchemy import create_engine, text
      from switchyard.db.migrate import alembic_config
      url = os.environ["SWITCHYARD_DATABASE_URL"]
      head = ScriptDirectory.from_config(alembic_config(url)).get_current_head()
      for _ in range(600):
          try:
              with create_engine(url).connect() as c:
                  cur = c.execute(text("select version_num from alembic_version")).scalar()
              if cur == head:
                  sys.exit(0)
              print("schema at", cur, "waiting for", head, flush=True)
          except Exception as e:
              print("waiting for the database:", type(e).__name__, flush=True)
          time.sleep(3)
      sys.exit(1)
  env:
{{ include "sy.dbEnv" . | indent 4 }}
  securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: [ALL]}}
{{- end -}}
