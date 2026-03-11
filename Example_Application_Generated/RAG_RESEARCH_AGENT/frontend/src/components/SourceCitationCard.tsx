import React from 'react';

interface Props { content: string; }

export function SourceCitationCard({ content }: Props) {
  // Extract [Source N] patterns from agent response
  const sourcePattern = /\[Source \d+\]\s*\(score: [\d.]+\)\s*(s3:\/\/[^\n]+)/g;
  const sources: { label: string; uri: string }[] = [];
  let match;
  while ((match = sourcePattern.exec(content)) !== null) {
    sources.push({ label: match[0].split(')')[0] + ')', uri: match[1] });
  }

  if (sources.length === 0) return null;

  return (
    <div style={{ marginTop: 8, padding: 8, backgroundColor: '#e8f5e9', borderRadius: 6, fontSize: 12 }}>
      <strong>Sources:</strong>
      {sources.map((s, i) => (
        <div key={i} style={{ marginTop: 4, color: '#2e7d32', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {s.label} — {s.uri.split('/').pop()}
        </div>
      ))}
    </div>
  );
}
