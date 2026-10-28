import {md5, toBase64} from './md5';

self.onmessage = async (event: MessageEvent<{id: number; blob: Blob}>) => {
  const {id, blob} = event.data;
  try {
    const digest = md5(new Uint8Array(await blob.arrayBuffer()));
    self.postMessage({id, md5: toBase64(digest)});
  } catch (error) {
    self.postMessage({id, error: String(error)});
  }
};
