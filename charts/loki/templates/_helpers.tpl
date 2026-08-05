{{- define "loki.fullname" -}}
{{- printf "%s-loki" .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end }}
