/* Local reconstruction of the paper's outer-loop switch. The public
 * artifact's radio loop stays intact. One QUIC connection per process. */
static int
StudyOuterUpdate(
    uint64_t now, int fresh, double ratio, double slope,
    uint64_t srtt, uint64_t minRtt)
{
    static uint64_t highSince, lastSample, overuseSince, quietSince, lastCut;
    static int active;
    if (!IsCellularControlEnabled() || g_RatioPacingRate <= 0) {
        return 0;
    }
    if (fresh) {
        if (lastSample == 0 || now-lastSample > 30000) {
            highSince = 0;
        }
        lastSample = now;
        if (ratio <= 0.2) {
            highSince = 0;
        } else if (highSince == 0) {
            highSince = now;
        }
    }
    int hasRadioHeadroom = highSince && now-highSince >= 100000 &&
        now-lastSample <= 30000 && ratio > 0.2;
    int overuse = slope > 0.05 && srtt > minRtt && srtt-minRtt >= 10000;
    if (overuse) {
        if (!overuseSince) overuseSince = now;
        quietSince = 0;
    } else {
        overuseSince = 0;
        if (!quietSince) quietSince = now;
    }

    const char* action = NULL;
    if (active && (!hasRadioHeadroom || (quietSince && now-quietSince >= 100000))) {
        active = 0;
        action = "exit";
    } else if (hasRadioHeadroom && overuseSince && now-overuseSince >= 15000 &&
               (!lastCut || now-lastCut >= 100000)) {
        action = active ? "cut" : "enter";
        active = 1;
        lastCut = now;
        g_RatioPacingRate *= 0.85;
        if (g_RatioPacingRate < 100000.0) g_RatioPacingRate = 100000.0;
    }
    if (action) {
        const char* filename = getenv("GBR_OUTER_LOG");
        FILE* log = filename ? fopen(filename, "a") : NULL;
        if (log) {
            if (ftell(log) == 0) {
                fputs("monotonic_us,action,ratio,ack_trend_slope,srtt_us,min_rtt_us,pace_Bps\n", log);
            }
            fprintf(log, "%llu,%s,%.6f,%.6f,%llu,%llu,%.2f\n",
                (unsigned long long)now, action, ratio, slope,
                (unsigned long long)srtt, (unsigned long long)minRtt, g_RatioPacingRate);
            fclose(log);
        }
    }
    return active;
}
