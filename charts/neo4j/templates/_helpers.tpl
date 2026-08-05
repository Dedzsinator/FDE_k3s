{{- define "neo4j.fullname" -}}
{{- printf "%s-neo4j" .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "neo4j.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/name: neo4j
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "neo4j.selectorLabels" -}}
app.kubernetes.io/name: neo4j
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
