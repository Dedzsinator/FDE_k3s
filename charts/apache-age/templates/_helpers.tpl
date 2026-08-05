{{- define "apache-age.fullname" -}}
{{- printf "%s-apache-age" .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "apache-age.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/name: apache-age
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "apache-age.selectorLabels" -}}
app.kubernetes.io/name: apache-age
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
