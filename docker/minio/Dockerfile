# MinIO 공식 Docker 이미지 배포가 중단돼(2025) Chainguard 가 소스에서 빌드한 이미지를 씁니다.
# 헬스체크·버킷 생성에 쓰려고 MinIO 클라이언트(mc)를 같이 넣습니다.
FROM cgr.dev/chainguard/minio-client:latest AS mc
FROM cgr.dev/chainguard/minio:latest
COPY --from=mc /usr/bin/mc /usr/bin/mc
