<script setup lang="ts">
import { ref } from "vue";
import { useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { useAuthStore } from "@/stores/auth";
import { useUiStore } from "@/stores/ui";
import { ApiError } from "@/api/client";

const { t } = useI18n();
const router = useRouter();
const authStore = useAuthStore();
const ui = useUiStore();

const username = ref("");
const password = ref("");
const error = ref("");
const loading = ref(false);

async function submit() {
  if (loading.value) return; // guard: @keyup.enter + form submit can both fire
  error.value = "";
  loading.value = true;
  try {
    await authStore.login(username.value.trim(), password.value);
    await router.push("/overview");
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : t("auth.badCredentials");
  } finally {
    loading.value = false;
  }
}
</script>
<template>
  <div class="login-page">
    <section class="login-story" aria-labelledby="login-story-title">
      <div class="login-brand">
        <svg
          width="34"
          height="34"
          viewBox="0 0 34 34"
          fill="none"
          aria-hidden="true"
        >
          <rect width="34" height="34" rx="10" fill="#6E8DFF" />
          <path
            d="M9 24 16 9h3l7 15h-5l-1.3-3H15l1.5-3.6h1.6L17.4 15 13.5 24H9Z"
            fill="white"
          />
        </svg>
        <strong>
          ARISE
          <span>COMPUTE</span>
        </strong>
      </div>
      <div class="story-copy">
        <div class="story-eyebrow">
          <span />
          {{ t("console.secureWorkspace") }}
        </div>
        <h1 id="login-story-title">{{ t("console.loginHero") }}</h1>
        <p>{{ t("console.loginHeroDesc") }}</p>
      </div>
      <div class="compute-art" aria-hidden="true">
        <svg viewBox="0 0 520 285" fill="none">
          <defs>
            <linearGradient id="compute-top" x1="0" y1="0" x2="1" y2="1">
              <stop stop-color="#7394FF" />
              <stop offset="1" stop-color="#3F60C7" />
            </linearGradient>
            <linearGradient id="compute-side" x1="0" y1="0" x2="0" y2="1">
              <stop stop-color="#415FAF" />
              <stop offset="1" stop-color="#223763" />
            </linearGradient>
          </defs>
          <g stroke="#334363" stroke-width=".7">
            <path
              v-for="n in 9"
              :key="'x' + n"
              :d="`M ${n * 50 - 60} 135 L ${n * 50 + 165} 265`"
            />
            <path
              v-for="n in 9"
              :key="'y' + n"
              :d="`M ${n * 50 - 60} 265 L ${n * 50 + 165} 135`"
            />
          </g>
          <g
            v-for="(cube, i) in [
              { x: 125, y: 110 },
              { x: 245, y: 65 },
              { x: 365, y: 110 },
            ]"
            :key="i"
          >
            <path
              :d="`M ${cube.x - 51} ${cube.y} l 51 -29 51 29 -51 29 Z`"
              fill="url(#compute-top)"
              stroke="#91ABFF"
            />
            <path
              :d="`M ${cube.x - 51} ${cube.y} l 51 29 0 83 -51 -29 Z`"
              fill="url(#compute-side)"
              stroke="#526CA9"
            />
            <path
              :d="`M ${cube.x} ${cube.y + 29} l 51 -29 0 83 -51 29 Z`"
              fill="#233761"
              stroke="#526CA9"
            />
            <path
              v-for="n in 3"
              :key="n"
              :d="`M ${cube.x - 41} ${cube.y + 20 + n * 16} l 29 17`"
              stroke="#728CC7"
              stroke-width="2"
            />
            <circle :cx="cube.x + 36" :cy="cube.y + 73" r="2" fill="#8FD9C7" />
            <path
              :d="`M ${cube.x - 25} ${cube.y} l 25 -14 25 14 -25 14 Z`"
              stroke="#C2D1FF"
            />
          </g>
          <path
            d="M125 240v9l120 28 120-28v-9"
            stroke="#617FAC"
            stroke-dasharray="3 5"
          />
          <circle cx="245" cy="277" r="3" fill="#9BB6FF" />
        </svg>
      </div>
      <div class="story-features">
        <span>
          <icon-thunderbolt />
          {{ t("console.loginFeature1") }}
        </span>
        <span>
          <icon-safe />
          {{ t("console.loginFeature2") }}
        </span>
        <span>
          <icon-cloud />
          {{ t("console.loginFeature3") }}
        </span>
      </div>
      <div class="story-footer">
        ARISE COMPUTE PLATFORM
        <span>BUILD WHAT'S NEXT.</span>
      </div>
    </section>
    <section class="login-form-side">
      <div class="login-utility">
        <a-button
          type="text"
          class="theme-toggle"
          :aria-label="
            t(ui.theme === 'dark' ? 'theme.switchLight' : 'theme.switchDark')
          "
          :title="
            t(ui.theme === 'dark' ? 'theme.switchLight' : 'theme.switchDark')
          "
          :aria-pressed="ui.theme === 'dark'"
          @click="ui.toggleTheme()"
        >
          <icon-sun v-if="ui.theme === 'dark'" />
          <icon-moon v-else />
        </a-button>
        <a-button
          type="text"
          :aria-label="t('lang.label')"
          @click="ui.setLang(ui.locale === 'zh' ? 'en' : 'zh')"
        >
          <icon-language />
          {{ ui.locale === "zh" ? "English" : "中文" }}
        </a-button>
      </div>
      <div class="login-card">
        <span class="login-form-eyebrow">{{ t("console.loginEyebrow") }}</span>
        <h2>{{ t("console.loginTitle") }}</h2>
        <p class="login-description">{{ t("console.loginDesc") }}</p>
        <a-form
          :model="{ username, password }"
          layout="vertical"
          @submit="submit"
        >
          <a-form-item :label="t('auth.username')" field="username">
            <a-input
              v-model="username"
              :placeholder="t('auth.username')"
              allow-clear
              size="large"
              :input-attrs="{
                'aria-label': t('auth.username'),
                autocomplete: 'username',
                autofocus: true,
              }"
            >
              <template #prefix><icon-user /></template>
            </a-input>
          </a-form-item>
          <a-form-item :label="t('auth.password')" field="password">
            <a-input-password
              v-model="password"
              :placeholder="t('auth.password')"
              size="large"
              :input-attrs="{
                'aria-label': t('auth.password'),
                autocomplete: 'current-password',
              }"
            >
              <template #prefix><icon-lock /></template>
            </a-input-password>
          </a-form-item>
          <a-alert v-if="error" type="error" class="login-error" role="alert">
            {{ error }}
          </a-alert>
          <a-button
            type="primary"
            long
            :loading="loading"
            :disabled="!username.trim() || !password"
            html-type="submit"
            size="large"
          >
            {{ t("auth.signInAction") }}
            <icon-arrow-right />
          </a-button>
        </a-form>
        <div class="login-support">
          <icon-info-circle />
          <p>{{ t("console.loginSupport") }}</p>
        </div>
      </div>
      <div class="login-bottom">
        <icon-lock />
        {{ t("console.secureWorkspace") }}
      </div>
    </section>
  </div>
</template>
<style scoped>
.login-page {
  display: grid;
  grid-template-columns: 1fr 1fr;
  min-height: 100vh;
  background: var(--surface);
}
.login-story {
  background: #142039;
  color: #fff;
  min-height: 100vh;
  position: relative;
  overflow: hidden;
  padding: 44px 9% 28px;
  display: flex;
  flex-direction: column;
}
.login-story:after {
  content: "";
  position: absolute;
  width: 600px;
  height: 600px;
  border-radius: 50%;
  border: 1px solid #ffffff04;
  top: 150px;
  left: -230px;
  pointer-events: none;
}
.login-brand {
  display: flex;
  align-items: center;
  gap: 12px;
}
.login-brand strong {
  font-size: 1.3125rem;
  letter-spacing: 1px;
}
.login-brand strong span {
  font-size: var(--text-xs);
  display: block;
  letter-spacing: 3.8px;
  font-weight: 400;
  color: #a1b2d2;
  margin-top: 3px;
}
.story-copy {
  margin-top: clamp(50px, 10vh, 120px);
  position: relative;
  z-index: 1;
}
.story-eyebrow {
  font-size: var(--text-xs);
  letter-spacing: 1px;
  color: #9cb1df;
  display: flex;
  align-items: center;
  gap: 8px;
}
.story-eyebrow > span {
  width: 5px;
  height: 5px;
  background: #88a6ff;
  border-radius: 50%;
}
.story-copy h1 {
  font-size: clamp(32px, 3.2vw, 49px);
  line-height: 1.5;
  letter-spacing: -1px;
  font-weight: 600;
  white-space: pre-line;
  margin: 22px 0 18px;
}
.story-copy p {
  font-size: var(--text-sm);
  line-height: 2.1;
  color: #8f9fbd;
  max-width: 380px;
  margin: 0;
}
.compute-art {
  width: 100%;
  max-width: 510px;
  margin: 15px auto 10px;
  flex: 1;
  display: flex;
  align-items: center;
}
.compute-art svg {
  width: 100%;
  max-height: 310px;
}
.story-features {
  display: flex;
  gap: 20px;
  flex-wrap: wrap;
  padding: 18px 0 30px;
  border-bottom: 1px solid #ffffff12;
}
.story-features > span {
  font-size: var(--text-xs);
  display: flex;
  align-items: center;
  gap: 7px;
  color: #9aacca;
}
.story-features svg {
  color: #7896d9;
}
.story-footer {
  font-size: var(--text-xs);
  letter-spacing: 1.5px;
  color: #a8b9d5;
  padding-top: 23px;
  display: flex;
  justify-content: space-between;
  gap: 10px;
}
.login-form-side {
  min-height: 100vh;
  position: relative;
  display: grid;
  place-items: center;
  padding: 100px 40px;
}
.login-utility {
  position: absolute;
  right: 34px;
  top: 34px;
}
.login-utility :deep(.arco-btn) {
  color: var(--muted);
  font-size: var(--text-sm);
  gap: 7px;
}
.login-card {
  width: 100%;
  max-width: 350px;
}
.login-form-eyebrow {
  font-size: var(--text-xs);
  letter-spacing: 2.5px;
  color: var(--accent);
  font-weight: 600;
}
.login-card h2 {
  font-size: 1.9375rem;
  font-weight: 600;
  letter-spacing: -0.8px;
  margin: 18px 0 10px;
}
.login-description {
  font-size: var(--text-sm);
  color: var(--muted);
  margin: 0 0 35px;
}
.login-card :deep(.arco-form-item) {
  margin-bottom: 23px;
}
.login-card :deep(.arco-input-wrapper) {
  padding: 5px 13px;
}
.login-card :deep(.arco-input-prefix) {
  color: var(--muted);
  margin-right: 11px;
}
.login-card :deep(.arco-btn-primary) {
  height: 44px;
  margin-top: 8px;
  display: flex;
  justify-content: space-between;
  padding: 0 18px;
  font-size: var(--text-sm);
}
.login-support {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  border-top: 1px solid var(--line);
  padding-top: 23px;
  margin-top: 32px;
  color: var(--muted);
}
.login-support p {
  font-size: var(--text-xs);
  line-height: 1.9;
  margin: 0;
}
.login-support svg {
  flex: none;
  margin-top: 3px;
  font-size: var(--text-base);
}
.login-bottom {
  position: absolute;
  bottom: 30px;
  font-size: var(--text-xs);
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--muted);
}
.login-error {
  margin-bottom: 18px;
}
@media (max-width: 1000px) {
  .login-story {
    padding: 35px 9% 25px;
  }
  .story-copy h1 {
    font-size: 2rem;
  }
  .story-features {
    flex-direction: column;
    gap: 12px;
  }
  .story-footer span {
    display: none;
  }
  .story-copy {
    margin-top: 80px;
  }
}
@media (max-width: 767px) {
  .login-page {
    grid-template-columns: 1fr;
  }
  .login-story {
    display: none;
  }
  .login-form-side {
    padding: 90px 28px;
  }
  .login-card {
    max-width: 380px;
  }
  .login-utility {
    top: 24px;
    right: 18px;
  }
  .login-bottom {
    bottom: 24px;
  }
}
</style>
