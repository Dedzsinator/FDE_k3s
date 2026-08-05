{{- define "ignition.fullname" -}}
{{- printf "%s-ignition" .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "ignition.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/name: ignition
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "ignition.selectorLabels" -}}
app.kubernetes.io/name: ignition
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
