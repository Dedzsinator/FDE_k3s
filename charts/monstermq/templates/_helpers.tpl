{{- define "monstermq.fullname" -}}
{{- printf "%s" .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "monstermq.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/name: monstermq
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "monstermq.selectorLabels" -}}
app.kubernetes.io/name: monstermq
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "monstermq.postgres.selectorLabels" -}}
app.kubernetes.io/name: monstermq-postgres
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
