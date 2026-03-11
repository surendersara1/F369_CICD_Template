import React, { useRef, useState } from 'react';
import { fetchAuthSession } from 'aws-amplify/auth';
import { getConfig } from '../config/runtime';

export function FileUpload() {
  const fileRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const config = getConfig();

  const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploading(true);
    try {
      const auth = await fetchAuthSession();
      const token = auth.tokens?.idToken?.toString() || '';
      const reader = new FileReader();
      reader.onload = async () => {
        const base64 = (reader.result as string).split(',')[1];
        await fetch(`${config.api.rest_endpoint}documents/upload`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
          body: JSON.stringify({ filename: file.name, content_base64: base64 }),
        });
        alert(`Uploaded: ${file.name}`);
      };
      reader.readAsDataURL(file);
    } catch (err) {
      console.error('Upload failed:', err);
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = '';
    }
  };

  return (
    <>
      <input ref={fileRef} type="file" onChange={handleUpload} style={{ display: 'none' }}
        accept=".pdf,.docx,.txt,.html,.md" aria-label="Upload document" />
      <button onClick={() => fileRef.current?.click()} disabled={uploading}
        aria-label="Upload document to knowledge base">
        {uploading ? 'Uploading...' : 'Upload Doc'}
      </button>
    </>
  );
}
