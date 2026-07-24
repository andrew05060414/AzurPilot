<template>
  <iframe
    class="alas"
    :src="url"
  />
</template>

<script lang="ts">
  import {defineComponent} from 'vue';

  const WEBUI_URL = (import.meta.env.VITE_WEBUI_URL as string | undefined)
    ?? 'http://127.0.0.1:22267';

  export default defineComponent({
    name: 'Alas',
    computed: {
      url: function () {
        return WEBUI_URL;
      },
    },
  });
</script>

<style scoped>
  .alas {
    border-width: 0;
    width: 100vw;
    height: 100vh;
    overflow: hidden;
  }
</style>
