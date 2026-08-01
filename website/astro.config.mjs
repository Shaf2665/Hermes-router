// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

// https://astro.build/config
export default defineConfig({
	site: 'https://hermes-router.vercel.app',
	integrations: [
		starlight({
			title: 'hermes-router',
			description: 'A self-hosted multi-provider AI router with key rotation, failover, and a built-in dashboard.',
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
						{ label: 'Getting Started', slug: 'getting-started' },
						{ label: 'Core Concepts', slug: 'concepts' },
						{ label: 'Routing Features', slug: 'routing' },
					],
				},
				{
					label: 'Set It Up',
					items: [
						{ label: 'Deployment', slug: 'deployment' },
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
						{ label: 'VS Code Extension', slug: 'vscode-extension' },
					],
				},
			],
		}),
	],
});
