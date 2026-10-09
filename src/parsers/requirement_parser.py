from dataclasses import dataclass
from pathlib import Path

from fastapi import UploadFile
from exmgai import Client
from src.schemas.inputs import Requirements


PROMPT = """
添付された PDF の要求仕様書を読み取り、記載されている要求を構造化してください。

- 要求は `items` に格納してください。
- 上位要求にぶら下がる下位要求は `sub_requirements` に入れてください。
- `text` には要求内容そのものを詳細に日本語で記述してください。
- 特に振る舞いに着目して、システムがどのように動作すべきかを明確にしてください。
- 文書に明示されていない推測は追加しないでください。
- 同じ意味の要求が重複している場合は統合してください。
- 下位要求が存在しない場合は空配列を返してください。
"""


@dataclass
class _UploadFileAdapter:

    upload: UploadFile
    fallback_filename: str

    @property
    def filename(self) -> str:
        return self.upload.filename or self.fallback_filename

    def read(self) -> bytes:
        self.upload.file.seek(0)
        data = self.upload.file.read()
        self.upload.file.seek(0)
        return data


def parse_pdf(uploads: list[UploadFile]) -> Requirements:
    if not uploads:
        return Requirements(items=[])

    files: list[_UploadFileAdapter] = []
    for index, upload in enumerate(uploads, start=1):
        filename = upload.filename or f"upload_{index}.pdf"
        if Path(filename).suffix.lower() != ".pdf":
            raise ValueError(f"Unsupported file extension: {filename}")
        files.append(_UploadFileAdapter(upload=upload, fallback_filename=filename))

    client = Client("gpt-5.4")
    response = client.chat.create(
        PROMPT,
        response_format=Requirements,
        files=files,
    )
    return Requirements.model_validate(response.content)

