{{- define "emqx.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "emqx.labels" -}}
app.kubernetes.io/name: emqx
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app: {{ include "emqx.fullname" . }}
{{- end }}
