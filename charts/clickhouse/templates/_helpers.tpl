{{- define "clickhouse.fullname" -}}
{{- printf "%s-clickhouse" .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "clickhouse.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/name: clickhouse
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "clickhouse.selectorLabels" -}}
app.kubernetes.io/name: clickhouse
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
