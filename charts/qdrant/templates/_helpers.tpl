{{- define "qdrant.fullname" -}}
{{- printf "%s-qdrant" .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "qdrant.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/name: qdrant
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "qdrant.selectorLabels" -}}
app.kubernetes.io/name: qdrant
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
