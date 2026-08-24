{{- define "flyt-adapter.name" -}}
flyt-adapter
{{- end }}
{{- define "flyt-adapter.labels" -}}
app.kubernetes.io/name: {{ include "flyt-adapter.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}
