{{- define "nats.fullname" -}}
{{- printf "%s-nats" .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "nats.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/name: nats
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "nats.selectorLabels" -}}
app.kubernetes.io/name: nats
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
