import type { ApiClient } from "../api/client";
import type { StoryVersionInfo } from "../api/types";
import { ArtifactText } from "./ArtifactText";

export function StoryViewer({ client, story }: { client: ApiClient; story: StoryVersionInfo | null }) {
  if (!story || !story.content_path) return <p>Story not generated yet</p>;
  return (
    <div>
      <p>Version {story.version_number}: {story.title} ({story.word_count} words)</p>
      <ArtifactText client={client} path={story.content_path} emptyLabel="The story text is empty" />
    </div>
  );
}
