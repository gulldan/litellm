import { describe, expect, it } from "vitest";
import { buildSkillMetadata, buildSkillSource, skillFormValues } from "./skillForm";
import type { Plugin } from "./types";

const plugin: Plugin = {
  id: "skill-1",
  name: "shared-skill",
  version: "2.7.4",
  description: "Published instructions",
  source: { source: "github", repo: "example/skills", ref: "release", sha: "pinned-commit" },
  author: { name: "Maintainer", email: "maintainer@example.org" },
  homepage: "https://docs.example.org/skills",
  keywords: ["workflow", "tools"],
  category: "Development",
  domain: "Engineering",
  namespace: "workflows",
  installation_preference: "available",
  enabled: true,
  created_at: null,
  updated_at: null,
};

describe("skill form payloads", () => {
  it("prefills published metadata and submits automatic version intent with the loaded timestamp", () => {
    const values = skillFormValues(plugin);
    const expectedValues = {
      version: "",
      domain: plugin.domain,
      namespace: plugin.namespace,
      authorName: plugin.author?.name,
      authorEmail: plugin.author?.email,
      keywords: "workflow, tools",
    };
    expect(values).toMatchObject(expectedValues);
    const { id, enabled, created_at, updated_at, source, ...metadata } = plugin;
    expect(buildSkillMetadata(values, plugin)).toEqual({
      ...metadata,
      version: null,
      expected_updated_at: plugin.updated_at,
    });
    expect(buildSkillSource(values, plugin)).toEqual(source);
  });

  it.each([
    { keywords: ["sales, finance", "review"] },
    { keywords: ["  workflow  ", "review", "review"] },
    { keywords: [] },
    { keywords: null },
  ])("preserves unchanged keywords through a description-only edit: $keywords", ({ keywords }) => {
    const published = { ...plugin, keywords };
    const values = { ...skillFormValues(published), description: "Updated instructions" };
    expect(buildSkillMetadata(values, published).keywords).toEqual(keywords);
  });

  it("parses an intentional keyword edit", () => {
    const values = { ...skillFormValues(plugin), keywords: "security, review" };
    expect(buildSkillMetadata(values, plugin).keywords).toEqual(["security", "review"]);
  });

  it.each([
    { source: "url", url: "https://github.com/example/skills.git", ref: "main", sha: "abc" },
    { source: "git-subdir", url: "ssh://git@git.example.org/team/skills.git", path: "skills/review", ref: "v2" },
    {
      source: "archive",
      url: "http://localhost:4000/claude-code/plugins/shared-skill/archive/aaa.zip",
      sha256: "a".repeat(64),
      revision: "approved",
    },
  ])("keeps unchanged source semantics and extras for $source metadata edits", (source) => {
    const published = { ...plugin, source };
    const values = { ...skillFormValues(published), description: "Proposed instructions" };
    expect(buildSkillSource(values, published)).toEqual(source);
    expect(buildSkillMetadata(values, published).description).toBe("Proposed instructions");
  });

  it("reparses changed sources and never carries old source extras to a replacement", () => {
    const values = {
      ...skillFormValues(plugin),
      skillUrl: "https://git.example.org/team/new.git",
      subPath: "skills/review",
    };
    expect(buildSkillSource(values, plugin)).toEqual({
      source: "git-subdir",
      url: "https://git.example.org/team/new.git",
      path: "skills/review",
    });
    expect(buildSkillSource({ ...values, skillUrl: "http://untrusted.example.org/new.zip" }, plugin)).toBeNull();
  });

  it("retains archive metadata when only the digest changes and supports removing a digest", () => {
    const published = {
      ...plugin,
      source: {
        source: "archive",
        url: "https://files.example.org/skill.zip",
        sha256: "a".repeat(64),
        revision: "reviewed",
      },
    };
    const values = skillFormValues(published);
    expect(buildSkillSource({ ...values, sha256: "B".repeat(64) }, published)).toEqual({
      ...published.source,
      sha256: "b".repeat(64),
    });
    expect(buildSkillSource({ ...values, sha256: "" }, published)).toEqual({
      source: "archive",
      url: published.source.url,
      revision: "reviewed",
    });
  });

  it("leaves default versions to publication and accepts an explicit override", () => {
    expect(buildSkillMetadata({ ...skillFormValues(), name: "new-skill" })).toEqual({ name: "new-skill" });
    const withoutVersion = { ...plugin, version: null };
    expect(buildSkillMetadata(skillFormValues(withoutVersion), withoutVersion).version).toBeNull();
    expect(buildSkillMetadata({ ...skillFormValues(plugin), version: "3.0.0" }, plugin).version).toBe("3.0.0");
  });

  it("clears edited metadata explicitly while preserving fields the form does not expose", () => {
    const clearedValues = {
      ...skillFormValues(plugin),
      description: "",
      authorName: "",
      authorEmail: "",
      keywords: "",
      domain: "",
      namespace: "",
      category: null,
    };
    const expectedMetadata = {
      description: null,
      author: null,
      keywords: [],
      domain: null,
      namespace: null,
      category: null,
      homepage: plugin.homepage,
      installation_preference: plugin.installation_preference,
      version: null,
    };
    expect(buildSkillMetadata(clearedValues, plugin)).toMatchObject(expectedMetadata);
  });
});
