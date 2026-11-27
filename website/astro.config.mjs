// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

// https://astro.build/config
export default defineConfig({
	site: 'https://hermes-router.vercel.app',
	integrations: [
		starlight({
			title: 'Hermes Router',
			description: 'Hermes Router is a self-hosted multi-provider AI gateway and model router with OpenAI-compatible and Anthropic-compatible APIs, automatic failover, and local model support.',
			logo: { src: './src/assets/logo.png', alt: 'hermes-router' },
			favicon: '/favicon.png',
			customCss: ['./src/styles/custom.css'],
			social: [
				{ icon: 'github', label: 'GitHub', href: 'https://github.com/Shaf2665/Hermes-router' },
			],
			sidebar: [
				{
					label: 'Start Here',
					items: [
						{ label: 'Introduction', slug: 'index' },
						{ label: 'Start Here', slug: 'getting-started' },
						{ label: 'Set Up on Your Computer', slug: 'setup' },
						{ label: 'Set Up on a Server', slug: 'deployment' },
						{ label: 'Connect VS Code', slug: 'vscode-extension' },
						{ label: 'Core Concepts', slug: 'concepts' },
						{ label: 'Routing Features', slug: 'routing' },
					],
				},
				{
					label: 'Set It Up',
					items: [
						{ label: 'InstaBox Hosting', slug: 'instabox' },
						{ label: 'Providers', slug: 'providers' },
						{ label: 'Free Model Rankings', slug: 'free-model-rankings' },
						{ label: 'Configuration', slug: 'configuration' },
					],
				},
				{
					label: 'Build With It',
					items: [
						{ label: 'Usage', slug: 'usage' },
						{ label: 'Build an Agent', slug: 'build-an-agent' },
					],
				},
				{
					label: 'Integrations',
					items: [
						{ label: 'Hall of Wisdom', slug: 'hall-of-wisdom' },
						{ label: 'Codex and Claude Code', slug: 'coding-clients' },
					],
				},
				{
					label: 'Operate',
					items: [
						{ label: 'Monitoring', slug: 'monitoring' },
					],
				},
				{
					label: 'Under the Hood',
					items: [
						{ label: 'How it works', slug: 'architecture' },
					],
				},
				{
					label: 'Tools',
					items: [
						{ label: 'Context Synchronization', slug: 'context-sync' },
					],
				},
			],
		}),
	],
});
