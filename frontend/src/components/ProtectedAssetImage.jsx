import { useEffect, useState } from 'react';
import { api } from '../api';
import { useAuthStore } from '../store/authStore';

export default function ProtectedAssetImage({ assetId, alt, className }) {
  const isAuthenticated = useAuthStore((state) => state.isAuthenticated);

  if (!isAuthenticated || !assetId) return null;

  return (
    <AuthenticatedAssetImage
      key={assetId}
      assetId={assetId}
      alt={alt}
      className={className}
    />
  );
}

function AuthenticatedAssetImage({ assetId, alt, className }) {
  const [src, setSrc] = useState(null);

  useEffect(() => {
    const controller = new AbortController();
    let objectUrl;

    api.get(`/assets/${assetId}/file`, {
      responseType: 'blob',
      signal: controller.signal,
    }).then(({ data }) => {
      objectUrl = URL.createObjectURL(data);
      setSrc(objectUrl);
    }).catch(() => {
      if (!controller.signal.aborted) setSrc(null);
    });

    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [assetId]);

  return src ? <img src={src} alt={alt} className={className} /> : null;
}
