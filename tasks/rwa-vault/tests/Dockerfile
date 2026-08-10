FROM rl-exploits-rwa-vault:latest

USER root
COPY test.sh /tests/test.sh
COPY ExploitGrader.sol /tests/ExploitGrader.sol
RUN chmod +x /tests/test.sh

ENV TASK_ID=rwa-vault
ENV KEEP_ARCHIVE_RPC=1

USER rltool
ENTRYPOINT []
CMD ["/tests/test.sh"]
