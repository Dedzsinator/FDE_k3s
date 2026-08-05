{{- define "predmaint.fullname" -}}
{{- printf "%s-predmaint" .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "predmaint.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/name: predmaint
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "predmaint.selectorLabels" -}}
app.kubernetes.io/name: predmaint
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
