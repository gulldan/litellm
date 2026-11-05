import { parseKeywords, parseSkillSource } from "./helpers";
import type { Plugin, PluginAuthor, SkillFormMetadata } from "./types";

export interface SkillFormValues {
  sourceType: "url" | "upload";
  archiveFile: File | null;
  skillUrl: string;
  subPath: string;
  sha256: string;
  name: string;
  domain: string;
  namespace: string;
  description: string;
  category: string | null;
  keywords: string;
  version: string;
  authorName: string;
  authorEmail: string;
}

export const skillFormValues = (plugin?: Plugin): SkillFormValues => {
  const metadata: Partial<Plugin> = plugin ?? {};
  const source: Plugin["source"] = metadata.source ?? {};
  const author: Partial<PluginAuthor> = metadata.author ?? {};
  return {
    sourceType: "url",
    archiveFile: null,
    skillUrl: source.source === "github" ? `https://github.com/${source.repo}` : source.url ?? "",
    subPath: source.path ?? "",
    sha256: source.sha256 ?? "",
    name: metadata.name ?? "",
    domain: metadata.domain ?? "",
    namespace: metadata.namespace ?? "",
    description: metadata.description ?? "",
    category: metadata.category ?? null,
    keywords: metadata.keywords?.join(", ") ?? "",
    version: "",
    authorName: author.name ?? "",
    authorEmail: author.email ?? "",
  };
};

type TextMetadataField = "version" | "description" | "domain" | "namespace";

const optionalTextMetadata = (
  field: TextMetadataField,
  value: string,
  includeEmpty: boolean,
): Pick<SkillFormMetadata, TextMetadataField> => (includeEmpty || value ? { [field]: value.trim() || null } : {});

export const buildSkillMetadata = (values: SkillFormValues, plugin?: Plugin): SkillFormMetadata => {
  const name = values.authorName.trim();
  const email = values.authorEmail.trim();
  const keywords =
    plugin && values.keywords === skillFormValues(plugin).keywords
      ? plugin.keywords ?? null
      : parseKeywords(values.keywords);
  return {
    name: plugin?.name ?? values.name.trim(),
    ...(plugin
      ? {
          homepage: plugin.homepage ?? null,
          installation_preference: plugin.installation_preference ?? null,
          expected_updated_at: plugin.updated_at,
        }
      : {}),
    ...optionalTextMetadata("version", values.version, Boolean(plugin)),
    ...optionalTextMetadata("description", values.description, Boolean(plugin)),
    ...(plugin || name ? { author: name ? { name, ...(email ? { email } : {}) } : null } : {}),
    ...(plugin || values.category ? { category: values.category || null } : {}),
    ...(plugin || values.keywords ? { keywords } : {}),
    ...optionalTextMetadata("domain", values.domain, Boolean(plugin)),
    ...optionalTextMetadata("namespace", values.namespace, Boolean(plugin)),
  };
};

export const buildSkillSource = (values: SkillFormValues, plugin?: Plugin): Plugin["source"] | null => {
  const original = skillFormValues(plugin);
  if (plugin && values.skillUrl === original.skillUrl && values.subPath === original.subPath) {
    if (values.sha256 === original.sha256) return plugin.source;
    const { sha256, ...source } = plugin.source;
    const digest = values.sha256.trim().toLowerCase();
    return digest ? { ...source, sha256: digest } : source;
  }
  const preview = parseSkillSource(values.skillUrl, values.subPath);
  if (!preview) return null;
  const digest = values.sha256.trim().toLowerCase();
  return {
    ...preview.parsed,
    ...(preview.parsed.source === "archive" && digest ? { sha256: digest } : {}),
  };
};
