{{- define "litmusedge.fullname" -}}
{{- printf "%s-litmusedge" .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "litmusedge.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/name: litmusedge
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "litmusedge.selectorLabels" -}}
app.kubernetes.io/name: litmusedge
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
