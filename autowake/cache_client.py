"""Restore a pinned model from the private cache; incomplete files never replace models."""
def restore_cached_model(path, name, base, worker_key, worker_id, gpu):
    import hashlib
    import time
    import requests
    meta = MODEL_CACHE_MANIFEST.get(name)
    if not meta:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + '.cache.part')
    for attempt in range(3):
        start = partial.stat().st_size if partial.exists() else 0
        if start > meta['size']:
            partial.unlink(); start = 0
        if start < meta['size']:
            headers = {'Authorization': 'Bearer ' + worker_key, 'X-H3-Worker-ID': worker_id, 'X-H3-GPU': gpu}
            if start:
                headers['Range'] = f'bytes={start}-'
            try:
                with requests.get(base + '/api/worker/model-cache/' + meta['sha256'], headers=headers,
                                  stream=True, timeout=(10, 60), allow_redirects=False) as response:
                    if response.status_code == 404:
                        return False
                    if response.status_code in (429, 503):
                        time.sleep(2 * (attempt + 1)); continue
                    response.raise_for_status()
                    if response.status_code != (206 if start else 200):
                        raise ValueError('Cache range not honored')
                    if (response.headers.get('X-Content-SHA256') != meta['sha256']
                            or int(response.headers.get('Content-Length', -1)) != meta['size'] - start):
                        raise ValueError('Cache metadata mismatch')
                    with partial.open('ab' if start else 'wb') as out:
                        count = start
                        for chunk in response.iter_content(1024 * 1024):
                            count += len(chunk)
                            if count > meta['size']:
                                raise ValueError('Cache response too long')
                            out.write(chunk)
            except requests.RequestException:
                continue
        if partial.exists() and partial.stat().st_size == meta['size']:
            with partial.open('rb') as stream:
                valid = hashlib.file_digest(stream, 'sha256').hexdigest() == meta['sha256']
            if not valid:
                partial.unlink()
                raise ValueError('Model cache checksum mismatch')
            partial.replace(path)
            print('持久缓存已恢复：', name, flush=True)
            return True
    print('持久缓存暂不可用，改用原始模型源：', name, flush=True)
    return False
