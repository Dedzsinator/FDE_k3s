{{- define "monitoring.fullname" -}}
{{- printf "%s-monitoring" .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
EMQX ServiceMonitor — scraped by Prometheus when EMQX is in the same namespace.
EMQX 5 exposes Prometheus metrics on port 18083 at /api/v5/prometheus/stats.
Deploy this after the EMQX chart is deployed.
*/}}
