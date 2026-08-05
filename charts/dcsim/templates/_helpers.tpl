{{- define "dcsim.fullname" -}}
{{- printf "%s-dcsim" .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "dcsim.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/name: dcsim
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "dcsim.selectorLabels" -}}
app.kubernetes.io/name: dcsim
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
