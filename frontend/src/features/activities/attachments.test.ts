import { describe, expect, it } from "vitest";

import { ApiError } from "@/lib/api/client";

import { extensionOf, fileKind, fileProblem, formatFileSize, pickFiles, STORAGE_DOWN, uploadProblem } from "./attachments";

const sized = (name: string, size: number) => ({ name, size });

describe("note files", () => {
  it("formats sizes in binary units", () => {
    expect(formatFileSize(820)).toBe("820 B");
    expect(formatFileSize(14 * 1024)).toBe("14 KB");
    expect(formatFileSize(1_258_291)).toBe("1.2 MB");
    expect(formatFileSize(10 * 1024 * 1024)).toBe("10 MB");
  });

  it("checks type, emptiness and size before an upload", () => {
    expect(fileProblem(sized("Quote.PDF", 10))).toBeNull();
    expect(fileProblem(sized("archive.zip", 10))).toMatch(/isn't allowed/);
    expect(fileProblem(sized(".pdf", 10))).toMatch(/isn't allowed/);
    expect(fileProblem(sized("noextension", 10))).toMatch(/isn't allowed/);
    expect(fileProblem(sized("a.txt", 0))).toBe("This file is empty.");
    expect(fileProblem(sized("a.txt", 10 * 1024 * 1024))).toBeNull();
    expect(fileProblem(sized("a.txt", 10 * 1024 * 1024 + 1))).toBe("Files can be at most 10 MB.");
  });

  it("accepts files up to the room left on the note", () => {
    const files = ["a.txt", "b.txt", "c.txt"].map((name) => new File(["x"], name));
    const { accepted, refused } = pickFiles(files, 2);
    expect(accepted.map((f) => f.name)).toEqual(["a.txt", "b.txt"]);
    expect(refused).toEqual(["c.txt: A note can have at most 10 files."]);
  });

  it("knows each type's icon", () => {
    expect(extensionOf("Photo.JPEG")).toBe("jpeg");
    expect(fileKind("jpeg")).toBe("image");
    expect(fileKind("csv")).toBe("sheet");
    expect(fileKind("docx")).toBe("text");
    expect(fileKind("bin")).toBe("other");
  });

  it("explains server refusals", () => {
    expect(uploadProblem(new ApiError(503, "storage_unavailable", "Down."))).toBe(STORAGE_DOWN);
    expect(uploadProblem(new ApiError(413, "file_too_large", "Files can be at most 10 MB."))).toBe("Files can be at most 10 MB.");
    expect(uploadProblem(new ApiError(400, "invalid_input", "Check.", { file: ["This file type isn't allowed."] }))).toBe(
      "This file type isn't allowed.",
    );
    expect(uploadProblem(new ApiError(422, "business_rule_violation", "A note can have at most 10 files."))).toBe(
      "A note can have at most 10 files.",
    );
  });
});
