#include "host_stubs.h"
#include SKETCH

static void assertSleep()
{
    assert(state.timer == 10800000000ULL);
    assert(state.pin == 36 && state.level == 0);
    assert(state.sleepCalls == 1);
    assert(state.log.find("deep-sleeping for 10800 seconds") != String::npos);
}

int main()
{
    const char *hints[] = {"2099-01-01T00:00:00Z", "2026-11-25T00:00:30Z",
                           "2001-01-01T00:00:00Z", "invalid", "", nullptr};
    for (const char *hint : hints) {
        for (bool fallback : {false, true}) {
            state = {};
            state.hint = hint;
            state.queueEmpty = fallback;
            setup();
            assertSleep();
            assert(state.wakePosts == 1 && state.manifestGets == int(fallback));
            assert(state.statusPosts == 1 && state.frames == 0);
            assert(state.log.find("wake cause: timer") != String::npos);
        }
    }

    state = {};
    state.cause = ESP_SLEEP_WAKEUP_EXT0;
    state.redraw = true;
    setup();
    assertSleep();
    assert(state.wakePosts == 1 && state.manifestGets == 0 && state.statusPosts == 1);
    assert(state.log.find("wake cause: wake-button") != String::npos);
    assert(state.imageGets == 1 && state.frames == 1);

    for (int failure : {0, 1, 2, 3, 4}) {
        state = {};
        state.online = failure != 0;
        state.wakeCode = failure >= 1 && failure <= 3 ? 503 : 200;
        state.manifestCode = failure == 1 ? 404 : 200;
        state.badManifest = failure == 2;
        state.emptyManifest = failure == 3;
        state.statusCode = failure == 4 ? 401 : 200;
        setup();
        assertSleep();
        assert(state.wakePosts == int(failure != 0));
        assert(state.manifestGets == int(failure >= 1 && failure <= 3));
        assert(state.statusPosts == int(failure == 4));
        assert(state.imageGets == 0 && state.frames == 0);
    }

    std::puts("PASS: Inkplate setup() fixed timer, WAKE, fallback and failures");
}
