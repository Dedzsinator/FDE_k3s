{{- define "timebase.fullname" -}}
{{- printf "%s-timebase" .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "timebase.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/name: timebase
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "timebase.historian.selectorLabels" -}}
app.kubernetes.io/name: timebase-historian
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "timebase.collector.selectorLabels" -}}
app.kubernetes.io/name: timebase-collector
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "timebase.explorer.selectorLabels" -}}
app.kubernetes.io/name: timebase-explorer
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
