{{- define "pgadmin.fullname" -}}
{{- printf "%s-pgadmin" .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "pgadmin.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/name: pgadmin
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "pgadmin.selectorLabels" -}}
app.kubernetes.io/name: pgadmin
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
