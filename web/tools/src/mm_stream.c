/* Streaming entry points for the multimon-ng WebAssembly build (used by the live pager feed).
 * Bypasses main(): open the named demodulators once, feed 22050 Hz s16 audio chunk by chunk,
 * and keep their state across calls (FLEX/POCSAG already decode incrementally). Output still goes
 * through verbprintf() -> stdout, which the JS side receives line by line via Module.print. */
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <emscripten/emscripten.h>
#include "multimon.h"

/* pager demodulators only (ALL_DEMOD includes the X11 scope, which is not built here) */
extern int aprs_mode;   /* hdlc.c: print AX.25 UI frames as "APRS: SRC>DST,PATH:payload" on one line */
static const struct demod_param *mm_dem[] = { &demod_flex, &demod_flex_next, &demod_poc5, &demod_poc12, &demod_poc24,
    &demod_afsk1200, &demod_dtmf, &demod_zvei1, &demod_zvei2, &demod_zvei3, &demod_ccir, &demod_eea, &demod_eia, &demod_morse };
#define MM_N (sizeof(mm_dem) / sizeof(mm_dem[0]))
static struct demod_state mm_st[MM_N];
static int mm_on[MM_N];
static unsigned mm_overlap;
static float *mm_fbuf;
static short *mm_sbuf;
static unsigned mm_cnt, mm_cap;

/* names: comma separated, e.g. "FLEX" or "FLEX,POCSAG1200". Returns the number of demodulators opened. */
EMSCRIPTEN_KEEPALIVE int mm_stream_open(const char *names) {
    int n = 0;
    mm_overlap = 0; aprs_mode = 1;
    for (unsigned i = 0; i < MM_N; i++) {
        mm_on[i] = 0;
        const char *p = names, *name = mm_dem[i]->name; size_t L = strlen(name);
        while (p && *p) {
            const char *e = strchr(p, ','); size_t l = e ? (size_t)(e - p) : strlen(p);
            if (l == L && strncasecmp(p, name, L) == 0) { mm_on[i] = 1; break; }
            p = e ? e + 1 : NULL;
        }
        if (!mm_on[i]) continue;
        memset(&mm_st[i], 0, sizeof mm_st[i]);
        mm_st[i].dem_par = mm_dem[i];
        if (mm_dem[i]->init) mm_dem[i]->init(&mm_st[i]);
        if (mm_dem[i]->overlap > mm_overlap) mm_overlap = mm_dem[i]->overlap;
        n++;
    }
    mm_cap = 65536 + mm_overlap; mm_cnt = 0;
    free(mm_fbuf); free(mm_sbuf);
    mm_fbuf = calloc(mm_cap, sizeof(float)); mm_sbuf = calloc(mm_cap, sizeof(short));
    return n;
}

/* 22050 Hz signed 16-bit audio, as main()'s input loop would see it from "-t raw". */
EMSCRIPTEN_KEEPALIVE void mm_stream_feed(const short *s16, int len) {
    while (len > 0) {
        int take = len; if ((unsigned)take > mm_cap - mm_cnt) take = mm_cap - mm_cnt;
        for (int k = 0; k < take; k++) { mm_sbuf[mm_cnt + k] = s16[k]; mm_fbuf[mm_cnt + k] = s16[k] * (1.0f / 32768.0f); }
        mm_cnt += take; s16 += take; len -= take;
        if (mm_cnt > mm_overlap) {
            unsigned use = mm_cnt - mm_overlap;
            for (unsigned i = 0; i < MM_N; i++)
                if (mm_on[i] && mm_dem[i]->demod) { buffer_t b = { mm_sbuf, mm_fbuf }; mm_dem[i]->demod(&mm_st[i], b, use); }
            memmove(mm_fbuf, mm_fbuf + use, mm_overlap * sizeof(float));
            memmove(mm_sbuf, mm_sbuf + use, mm_overlap * sizeof(short));
            mm_cnt = mm_overlap;
        }
    }
    fflush(stdout);
}

EMSCRIPTEN_KEEPALIVE void mm_stream_close(void) {
    for (unsigned i = 0; i < MM_N; i++) if (mm_on[i]) { if (mm_dem[i]->deinit) mm_dem[i]->deinit(&mm_st[i]); mm_on[i] = 0; }
}
