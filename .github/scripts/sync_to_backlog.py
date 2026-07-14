"""Sync documents from ./doc/ directory to Backlog documents."""

import os
import sys
from pathlib import Path

import requests

def parse_file_list(env_var: str) -> list[str]:
    """Parse a comma-separated file list from an environment variable."""
    value = os.environ.get(env_var, "")
    if not value:
        return []
    return [f.strip() for f in value.split(",") if f.strip()]

class BacklogClient:
    """Client for Backlog Document API."""

    def __init__(self, space_id: str, domain: str, api_key: str, project_id: int):
        self.base_url = f"https://{space_id}.{domain}/api/v2"
        self.api_key = api_key
        self.project_id = project_id

    def _params(self, **kwargs) -> dict:
        """Build query parameters with API key."""
        return {"apiKey": self.api_key, **kwargs}

    def get_documents(self, offset: int = 0, count: int = 100) -> list[dict]:
        """Fetch document list for the project."""
        resp = requests.get(
            f"{self.base_url}/documents",
            params=self._params(**{"projectId[]": self.project_id, "offset": offset, "count": count}),
        )
        resp.raise_for_status()
        return resp.json()

    def fetch_document_map(self) -> dict[str, dict]:
        """Fetch all documents and return a title-to-document mapping."""
        doc_map: dict[str, dict] = {}
        offset = 0
        count = 100
        while True:
            docs = self.get_documents(offset=offset, count=count)
            for doc in docs:
                doc_map[doc["title"]] = doc
            if len(docs) < count:
                break
            offset += count
        return doc_map

    def add_document(self, title: str, content: str) -> dict:
        """Create a new document."""
        resp = requests.post(
            f"{self.base_url}/documents",
            params=self._params(),
            data={
                "projectId": self.project_id,
                "title": title,
                "content": content,
                "addLast": "true",
            },
        )
        resp.raise_for_status()
        return resp.json()

    def delete_document(self, document_id: str) -> dict:
        """Delete a document by ID."""
        resp = requests.delete(
            f"{self.base_url}/documents/{document_id}",
            params=self._params(),
        )
        resp.raise_for_status()
        return resp.json()

def resolve_target_files(doc_dir: Path) -> list[Path]:
    """Determine which files to sync based on environment variables.

    If SYNC_MODE=diff and CHANGED_FILES is set, only those files are targeted.
    Otherwise, all files in doc_dir are targeted.
    """
    sync_mode = os.environ.get("SYNC_MODE", "all")

    if sync_mode == "diff":
        files = [Path(f) for f in parse_file_list("CHANGED_FILES")]
        # 存在しないファイル(削除されたファイル)は除外
        files = [f for f in files if f.is_file()]
        print(f"Mode: diff ({len(files)} changed files)")
    else:
        files = sorted(f for f in doc_dir.iterdir() if f.is_file())
        print(f"Mode: all ({len(files)} files)")

    return files

def resolve_deleted_titles() -> list[str]:
    """Determine which document titles should be deleted.

    Returns:
        List of titles (stem of deleted file paths) to remove from Backlog.
    """
    return [Path(f).stem for f in parse_file_list("DELETED_FILES")]

def delete_removed_documents(client: BacklogClient, doc_map: dict[str, dict]) -> tuple[int, int]:
    """Delete Backlog documents corresponding to deleted files.

    Returns:
        Tuple of (success_count, fail_count).
    """
    titles = resolve_deleted_titles()
    if not titles:
        return 0, 0

    print(f"Deleting {len(titles)} removed document(s)...")
    success = 0
    fail = 0

    for title in titles:
        try:
            existing = doc_map.get(title)
            if existing:
                client.delete_document(existing["id"])
                print(f"  Deleted: \"{title}\" (ID: {existing['id']})")
                success += 1
            else:
                print(f"  Not found on Backlog, skipping: \"{title}\"")
        except requests.HTTPError as e:
            print(f"  ERROR deleting \"{title}\": {e}")
            fail += 1

    return success, fail

def sync_documents(client: BacklogClient, doc_dir: Path, doc_map: dict[str, dict]) -> tuple[int, int]:
    """Sync target files to Backlog documents.

    Returns:
        Tuple of (success_count, fail_count).
    """
    success = 0
    fail = 0

    files = resolve_target_files(doc_dir)
    if not files:
        print("No files found in document directory.")
        return 0, 0

    for i, file_path in enumerate(files, start=1):
        title = file_path.stem
        content = file_path.read_text(encoding="utf-8")
        print(f"[{i}/{len(files)}] {file_path.name} -> \"{title}\"")

        try:
            # 既存ドキュメントがあれば削除
            existing = doc_map.get(title)
            if existing:
                print(f"  Deleting existing document (ID: {existing['id']})")
                client.delete_document(existing["id"])

            # 新規追加
            result = client.add_document(title, content)
            print(f"  Created document (ID: {result['id']})")
            success += 1

        except requests.HTTPError as e:
            print(f"  ERROR: {e}")
            fail += 1

    return success, fail

def main():
    # 環境変数から設定を取得
    required_vars = ["BACKLOG_API_KEY", "BACKLOG_SPACE_ID", "BACKLOG_PROJECT_ID"]
    missing = [v for v in required_vars if not os.environ.get(v)]
    if missing:
        print(f"ERROR: Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)

    api_key = os.environ["BACKLOG_API_KEY"]
    space_id = os.environ["BACKLOG_SPACE_ID"]
    project_id = int(os.environ["BACKLOG_PROJECT_ID"])
    domain = os.environ.get("BACKLOG_DOMAIN", "backlog.jp")
    doc_dir = Path(os.environ.get("DOC_DIR", "./doc"))

    print("=== Backlog Document Sync ===")
    print(f"Space: {space_id}.{domain}")
    print(f"Project ID: {project_id}")
    print(f"Document directory: {doc_dir}")
    print()

    if not doc_dir.is_dir():
        print(f"ERROR: Document directory '{doc_dir}' does not exist.")
        sys.exit(1)

    client = BacklogClient(space_id, domain, api_key, project_id)

    # ドキュメント一覧を一括取得してマップ化
    doc_map = client.fetch_document_map()

    # 削除されたファイルに対応するドキュメントを削除
    del_success, del_fail = delete_removed_documents(client, doc_map)

    # 変更・追加されたファイルを同期
    success, fail = sync_documents(client, doc_dir, doc_map)

    print()
    print(f"=== Sync Complete: Upserted={success}, Deleted={del_success}, Failed={fail + del_fail} ===")

    if fail + del_fail > 0:
        sys.exit(1)

if __name__ == "__main__":
    main()
