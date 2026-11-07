<script setup lang="ts">
import DefaultTheme from 'vitepress/theme'

const { Layout } = DefaultTheme
</script>

<template>
  <Layout>
    <template #home-hero-after>
      <nav class="home-badges" aria-label="GitHub project information">
        <a
          href="https://github.com/BigDawnGhost/wenyi/stargazers"
          target="_blank"
          rel="noopener noreferrer"
        >
          <img
            src="https://img.shields.io/github/stars/BigDawnGhost/wenyi?style=for-the-badge&label=Stars&labelColor=38362f&color=37636b&logo=github&logoColor=ede3d0"
            alt="GitHub stars"
            height="28"
            decoding="async"
            referrerpolicy="no-referrer"
          >
        </a>
        <a
          href="https://github.com/BigDawnGhost/wenyi/releases/latest"
          target="_blank"
          rel="noopener noreferrer"
        >
          <img
            src="https://img.shields.io/github/v/release/BigDawnGhost/wenyi?style=for-the-badge&label=Latest&labelColor=38362f&color=81643c"
            alt="Latest release"
            height="28"
            decoding="async"
            referrerpolicy="no-referrer"
          >
        </a>
        <a
          href="https://github.com/BigDawnGhost/wenyi/blob/main/LICENSE"
          target="_blank"
          rel="noopener noreferrer"
        >
          <img
            src="https://img.shields.io/github/license/BigDawnGhost/wenyi?style=for-the-badge&label=License&labelColor=38362f&color=626b47"
            alt="Open source license"
            height="28"
            decoding="async"
            referrerpolicy="no-referrer"
          >
        </a>
      </nav>
    </template>
  </Layout>
</template>
