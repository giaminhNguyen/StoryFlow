import { useState, type FormEvent } from "react";
import type { ApiClient } from "../api/client";
import { errorMessage } from "../format";

export function AddProjectForm({ client, workflowId, disabled, onAdded }: {
  client: ApiClient; workflowId: string; disabled: boolean; onAdded: () => Promise<void> | void;
}) {
  const [title, setTitle] = useState("");
  const [slug, setSlug] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (busy || disabled || !title.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const input: { title: string; slug?: string } = { title: title.trim() };
      if (slug.trim()) input.slug = slug.trim();
      await client.addProject(workflowId, input);
      setTitle("");
      setSlug("");
      await onAdded();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={(e) => void submit(e)} aria-label="Add project" className="card form">
      <h3>Add project</h3>
      {disabled && <p className="muted">Projects cannot be added to a finished or cancelled workflow.</p>}
      <label>Title
        <input value={title} onChange={(e) => setTitle(e.target.value)} required disabled={disabled || busy} />
      </label>
      <label>Slug (optional)
        <input value={slug} onChange={(e) => setSlug(e.target.value)} disabled={disabled || busy} />
      </label>
      <button type="submit" disabled={disabled || busy || !title.trim()}>{busy ? "Adding..." : "Add project"}</button>
      {error && <div role="alert" className="banner banner-error">{error}</div>}
    </form>
  );
}
