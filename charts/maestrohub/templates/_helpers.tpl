{{- define "maestrohub.fullname" -}}
{{- printf "%s-maestrohub" .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "maestrohub.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/name: maestrohub
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "maestrohub.selectorLabels" -}}
app.kubernetes.io/name: maestrohub
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
