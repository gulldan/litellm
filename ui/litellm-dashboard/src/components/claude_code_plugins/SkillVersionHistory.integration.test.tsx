import { fireEvent, screen } from "@testing-library/react";
import { renderWithProviders, testQueryClient } from "@/../tests/test-utils";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import SkillVersionHistory from "./SkillVersionHistory";
import type { Plugin } from "./types";

const skill: Plugin = {
  id: "versioned-id",
  name: "versioned",
  source: { source: "github", repo: "example/skill" },
  enabled: true,
  version: "1.0.1",
  description: null,
  created_at: null,
  updated_at: null,
};
const fetchMock = vi.fn<typeof fetch>();
beforeEach(() => {
  testQueryClient.clear();
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

it("distinguishes retained bytes from external metadata and paginates publications", async () => {
  fetchMock.mockResolvedValueOnce(
    Response.json({
      versions: [
        {
          id: "new",
          version: "1.0.1",
          description: "Revised instructions",
          manifest: { source: skill.source },
          published_at: null,
          published_by: null,
          content_kind: "source_reference",
          archive_path: null,
        },
        {
          id: "old",
          version: "1.0.0",
          description: null,
          manifest: {},
          published_at: null,
          published_by: null,
          content_kind: "stored_archive",
          archive_path: "/claude-code/plugins/versioned/versions/old/archive",
        },
      ],
      next_offset: 20,
    }),
  );
  renderWithProviders(<SkillVersionHistory skill={skill} accessToken="test-token" />);
  expect(await screen.findByText("1.0.1")).toBeInTheDocument();
  expect(screen.getByText(/External files may change/)).toBeInTheDocument();
  expect(screen.getByText(/Uploaded ZIP retained/)).toBeInTheDocument();
  expect(screen.getAllByRole("button", { name: "Download retained ZIP" })).toHaveLength(1);
  fetchMock.mockResolvedValueOnce(Response.json({ versions: [], next_offset: null }));
  fireEvent.click(screen.getByRole("button", { name: "Older" }));
  expect(await screen.findByText(/No recorded releases yet/)).toBeInTheDocument();
  expect(screen.queryByText("1.0.1")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Older" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Newer" })).toBeEnabled();
});
