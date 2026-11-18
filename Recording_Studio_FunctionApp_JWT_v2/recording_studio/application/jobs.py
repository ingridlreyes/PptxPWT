"""Contratos de trabajo de la Function App sin permitir rutas locales ni identidades del cliente."""
from pydantic import Field, SecretStr
from recording_studio.domain.models import Model, Theme


class JobRequest(Model):
    """Solicitud pequeña: URL, objetivo y opcionalmente subtítulos y credencial de origen."""
    video_url: str = Field(min_length=1, max_length=16000)
    brief: str = Field(min_length=1, max_length=12000)
    source_token: SecretStr | None = None
    subtitles: str | None = Field(default=None, max_length=1000000)
    theme: Theme = Field(default_factory=Theme)
    include_word: bool = True

    def private_payload(self):
        """Expone el secreto solo al cifrado de solicitudes; no al historial Durable ni a logs."""
        data = self.model_dump(exclude={"source_token"})
        data["source_token"] = self.source_token.get_secret_value() if self.source_token else None
        if data["source_token"] and (len(data["source_token"]) > 20000 or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in data["source_token"])):
            raise ValueError("Token de origen inválido")
        return data
