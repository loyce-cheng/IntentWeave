export type ArchiveReceipt = {
  id: string;
  token: string;
  name: string;
  schema_version: string;
  created_at: number;
  expires_at: number;
  byte_count: number;
  step_count: number;
};

export const ARCHIVE_STORAGE_KEY = "worldmind.archive-capabilities.v1";
const CAPABILITY_PATTERN = /^[A-Za-z0-9_-]{16,128}$/;

/** Local capabilities are deliberately separate from simulation exports and model access. */
export function readArchives(): ArchiveReceipt[] {
  const value: unknown = JSON.parse(
    localStorage.getItem(ARCHIVE_STORAGE_KEY) ?? "[]",
  );
  if (!Array.isArray(value))
    throw new Error("本地存档索引损坏，请先导出可用记录。");
  if (value.length > 64) throw new Error("本地存档索引超出容量。");
  for (const item of value) {
    if (
      !item ||
      typeof item !== "object" ||
      !CAPABILITY_PATTERN.test(item.id) ||
      !CAPABILITY_PATTERN.test(item.token) ||
      typeof item.name !== "string" ||
      item.name.length > 80 ||
      typeof item.schema_version !== "string" ||
      item.schema_version.length > 80 ||
      ![
        item.created_at,
        item.expires_at,
        item.byte_count,
        item.step_count,
      ].every((n) => typeof n === "number" && Number.isFinite(n) && n >= 0)
    )
      throw new Error("本地存档索引损坏，请先导出可用记录。");
  }
  return value;
}

export function writeArchives(archives: ArchiveReceipt[]) {
  if (archives.length > 64)
    throw new Error("本地存档索引已满，请先删除不需要的存档。");
  localStorage.setItem(ARCHIVE_STORAGE_KEY, JSON.stringify(archives));
}
