{{- define "heimdall.fullname" -}}
{{- printf "%s-heimdall" .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "heimdall.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/name: heimdall
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "heimdall.selectorLabels" -}}
app.kubernetes.io/name: heimdall
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
