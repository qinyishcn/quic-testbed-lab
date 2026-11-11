/* Local streaming bridge between the QUIC server and the inference worker.
 * Each request and answer is private to the working directory of the run. */
#include <fcntl.h>

static uint64_t clock_us(void);
static picoquic_cnx_t *online_connection;
static size_t pending_uploads;
static int online_ready[MAX_TASKS], online_finished[MAX_TASKS], online_first[MAX_TASKS];
static off_t online_reply_offset[MAX_TASKS];

static int online_write(const char *filename, const uint8_t *bytes, size_t length)
{
    FILE *file = fopen(filename, "ab");
    if (!file) return -1;
    size_t written = fwrite(bytes, 1, length, file);
    int closed = fclose(file);
    return written == length && closed == 0 ? 0 : -1;
}

static int online_upload(size_t index, const uint8_t *bytes, size_t length)
{
    char name[64];
    snprintf(name, sizeof(name), "request-%zu.partial", index);
    return online_write(name, bytes, length);
}

static int online_submit(picoquic_cnx_t *cnx, size_t index)
{
    char source[64], ready[64];
    snprintf(source, sizeof(source), "request-%zu.partial", index);
    snprintf(ready, sizeof(ready), "request-%zu.bin", index);
    if (rename(source, ready)) return -1;
    online_connection = cnx;
    online_ready[index] = 1;
    if (pending_uploads <= index) pending_uploads = index + 1;
    return 0;
}

static int online_receive(size_t index, const uint8_t *bytes, size_t length)
{
    if (!online_first[index]) {
        online_first[index] = 1;
        fprintf(events, "%zu,first,%lu,0,0\n", index, clock_us());
    }
    char name[64];
    snprintf(name, sizeof(name), "answer-%zu.txt", index);
    return online_write(name, bytes, length);
}

static int online_send(void)
{
    for (size_t index = 0; index < pending_uploads; index++) {
        if (!online_ready[index] || online_finished[index]) continue;
        char file[64], done[64];
        snprintf(file, sizeof(file), "reply-%zu.txt", index);
        snprintf(done, sizeof(done), "reply-%zu.done", index);
        /* Check the end marker before reading so no final bytes can be missed. */
        int complete = access(done, F_OK) == 0;
        int fd = open(file, O_RDONLY);
        if (fd < 0) {
            if (!complete && errno == ENOENT) continue;
            return -1;
        }
        uint8_t bytes[4096];
        ssize_t count = pread(fd, bytes, sizeof(bytes), online_reply_offset[index]);
        close(fd);
        if (count < 0) return -1;
        online_reply_offset[index] += count;
        int fin = complete && count == 0;
        if ((count || fin) && picoquic_add_to_stream(online_connection, 4 * index, bytes, count, fin))
            return -1;
        if (fin) online_finished[index] = 1;
    }
    return 0;
}
